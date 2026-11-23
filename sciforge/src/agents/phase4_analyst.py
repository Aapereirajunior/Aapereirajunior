"""Fase 4 — Bioestatístico R: renv + scripts modulares + Contrato de Diagnósticos + QA Visual.

Privacidade (Trava 5): em strict/hybrid TODAS as chamadas desta fase vão ao modelo local (Ollama);
logs de execução passam pelo sanitizador LGPD antes de voltar ao modelo; o código gerado passa por
análise estática que bloqueia impressão de registros individuais.

Scripts gerados em 02_analise/:
  00_setup.R, 01_prep_dados.R, 02_modelos_principais.R, 03_sensibilidade.R, 04_tabelas_figuras.R
Cada script é executado no sandbox com até 4 tentativas de auto-correção. Após o pipeline, o
Contrato de Diagnósticos (04_resultados_verificados.json) é validado e o QA visual de cada figura
roda por até 2 ciclos. Não-convergência ou perda amostral injustificada BLOQUEIAM o Portão #2.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ..core.budget_guard import AttemptLimitReached
from ..core.config import SCRIPTS_R_DIR
from ..core.llm_client import extract_code_block
from ..core.schemas import ResultadosVerificados
from ..tools.data_profiler import profile_to_prompt
from ..tools.r_sandbox import RSandbox, static_check
from ..tools.visual_inspector import inspect_figure
from .base import GateBlocked, PhaseAgent

SCRIPTS = [
    ("00_setup.R", "Carregar pacotes (library) com mensagens suprimidas, fixar set.seed, source() dos helpers "
                   "via Sys.getenv('SCIFORGE_R_HELPERS'), criar diretórios intermediarios/tabelas/figuras e "
                   "zerar 01_estado/04_resultados_verificados.json (escrever '{}')."),
    ("01_prep_dados.R", "Ler os dados de 00_semente/dados SOMENTE para leitura, aplicar critérios de "
                        "elegibilidade com sf_flow_start/sf_flow_exclude (um critério por chamada, com "
                        "justificativa), criar variáveis analíticas, gravar sf_flow_write() e "
                        "sf_missing_report() e salvar a base analítica em 02_analise/intermediarios/analitica.rds."),
    ("02_modelos_principais.R", "Ajustar os modelos principais do protocolo sobre intermediarios/analitica.rds; "
                                "gravar 'diagnosticos_modelos' (lista, um item por modelo, com converged explícito), "
                                "'resultados_principais' (sf_tidy_model) e 'descritivos' agregados; salvar os "
                                "objetos de modelo em intermediarios/modelos.rds."),
    ("03_sensibilidade.R", "Executar o plano de sensibilidade do protocolo; acrescentar diagnósticos com "
                           "sf_results_append('diagnosticos_modelos', ...) e gravar 'resultados_sensibilidade'."),
    ("04_tabelas_figuras.R", "Gerar TODAS as tabelas com gtsummary (Tabela 1 de características, tabela de "
                             "regressão) via sf_save_table('tbl-...', caption=) e TODAS as figuras ggplot2 via "
                             "sf_save_figure('fig-...', caption=) com sf_theme() e sf_palette(); registrar "
                             "'tabelas' e 'figuras' (labels) no JSON."),
]

CONTRACT = """CONTRATO OBRIGATÓRIO de 01_estado/04_resultados_verificados.json (use os helpers sf_*):
{
  "fluxo_amostral": {"n_inicial": int, "exclusoes": [{"criterio": str, "n_removido": int, "justificativa": str}], "n_final": int},
  "tratamento_missing": {"variaveis": [{"variavel": str, "pct_missing": num, "metodo": str}], "metodo_geral": str},
  "diagnosticos_modelos": [{"modelo": str, "converged": bool, "familia": str, "dispersao": num|null,
                            "superdispersao": bool|null, "aic": num|null, "qaic": num|null, "bic": num|null,
                            "r2": num|null, "auc": num|null, "calibracao": str|null, "pressupostos": {}, "avisos": []}],
  "resultados_principais": [{"modelo": str, "termo": str, "medida": str, "estimativa": num,
                             "ic95_inf": num, "ic95_sup": num, "p_valor": num, "n": int}],
  "resultados_sensibilidade": [ ...mesmo formato... ],
  "descritivos": {...apenas agregados...}, "tabelas": ["tbl-..."], "figuras": ["fig-..."]
}"""


def validate_results(path: Path, max_loss: float) -> tuple[Optional[ResultadosVerificados], list[str]]:
    """Valida o Contrato de Diagnósticos. Retorna (resultados, problemas bloqueantes)."""
    if not path.exists():
        return None, ["04_resultados_verificados.json não foi gerado"]
    try:
        res = ResultadosVerificados.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        return None, [f"Contrato inválido: {str(exc)[:2500]}"]
    problems = []
    for d in res.diagnosticos_modelos:
        if not d.converged:
            problems.append(f"Modelo '{d.modelo}' NÃO convergiu ({'; '.join(d.avisos) or 'sem detalhes'})")
        if d.superdispersao and (d.familia or "").lower() == "poisson":
            problems.append(f"Modelo '{d.modelo}' Poisson com superdispersão (φ={d.dispersao}); use quasipoisson/binomial negativa")
    fluxo = res.fluxo_amostral
    perda = 1 - fluxo.n_final / fluxo.n_inicial
    if perda > max_loss:
        sem_just = [e.criterio for e in fluxo.exclusoes if not (e.justificativa or "").strip()]
        if sem_just:
            problems.append(f"Perda amostral de {perda:.1%} (> {max_loss:.0%}) com exclusões sem justificativa: {sem_just}")
    # Sensibilidade pode legitimamente usar N maior (ex.: imputação múltipla); o principal não.
    for e in res.resultados_principais:
        if e.n is not None and e.n > fluxo.n_final:
            problems.append(f"Estimativa {e.modelo}/{e.termo} usa n={e.n} > N analítico {fluxo.n_final}")
    return res, problems


class Phase4Analyst(PhaseAgent):
    phase = "phase4"
    title = "Fase 4 — Análise reprodutível em R com contrato de diagnósticos"

    def _sandbox(self) -> RSandbox:
        timeout = int(self.ctx.settings.get("analysis", {}).get("rscript_timeout_s", 1800))
        return RSandbox(self.ctx.ws.root, self.ctx.ws.analise, timeout_s=timeout)

    async def run(self) -> dict:
        ws = self.ctx.ws
        analysis_cfg = self.ctx.settings.get("analysis", {})
        sandbox = self._sandbox()
        if not sandbox.available:
            raise RuntimeError("Rscript não encontrado. Instale o R para executar a Fase 4.")
        (ws.analise / "intermediarios").mkdir(parents=True, exist_ok=True)

        if analysis_cfg.get("use_renv", True):
            await self.cached_step("renv", lambda: self._init_renv(analysis_cfg))

        inv = self.ctx.inventario()
        protocolo = ws.protocolo.read_text(encoding="utf-8")
        schema_txt = "\n\n".join(profile_to_prompt(p) for p in inv.schema_dados)
        data_files = [str(p.relative_to(ws.root)) for p in ws.data_files()]
        helpers = (SCRIPTS_R_DIR / "export_table_helper.R").read_text(encoding="utf-8")
        helper_api = "\n".join(line for line in helpers.splitlines() if line.startswith(("sf_", "#   sf_")) or "<- function" in line)
        base_ctx = (f"PROTOCOLO APROVADO NO PORTÃO #1:\n{protocolo[:25000]}\n\n"
                    f"PERFIL AGREGADO DOS DADOS (nunca há acesso a linhas individuais):\n{schema_txt[:12000]}\n\n"
                    f"ARQUIVOS DE DADOS (relativos ao workspace; use file.path(sf_paths()$root, ...)): {data_files}\n\n"
                    f"GLOSSÁRIO: {json.dumps(inv.glossario_obrigatorio, ensure_ascii=False)}\n"
                    f"CONTEXTO TÁCITO: {json.dumps(inv.contexto_tacito_local, ensure_ascii=False)}\n\n"
                    f"API DOS HELPERS (já carregados por source(Sys.getenv('SCIFORGE_R_HELPERS'))):\n{helper_api}\n\n{CONTRACT}")

        written: dict[str, str] = {}
        for name, purpose in SCRIPTS:
            path = ws.analise / name
            if not self.ctx.state.step_done(self.phase, f"script:{name}"):
                code = await self._write_script(name, purpose, base_ctx, written)
                path.write_text(code, encoding="utf-8")
                validator = self._post_validator(name)
                result = await sandbox.run_with_autofix(path, self._fixer(name, purpose, base_ctx),
                                                        budget=self.ctx.budget, extra_validator=validator)
                if not result.ok:
                    raise GateBlocked(f"{name} falhou após o limite de auto-correções:\n{result.feedback()[-3000:]}")
                self.ctx.state.save_step(self.phase, f"script:{name}", {"duracao_s": round(result.duration_s, 1)})
                self.ctx.log(f"  ✓ {name} executado ({result.duration_s:.1f}s)")
            written[name] = path.read_text(encoding="utf-8")

        res, problems = validate_results(ws.resultados, float(analysis_cfg.get("max_unexplained_sample_loss", 0.2)))
        if problems:
            raise GateBlocked("Contrato de Diagnósticos violado:\n- " + "\n- ".join(problems))

        qa = await self._figure_qa(sandbox, base_ctx)
        missing_tables = [t for t in res.tabelas if not (ws.tabelas / f"{t.replace('-', '_')}.rds").exists()]
        if missing_tables:
            raise GateBlocked(f"Tabelas declaradas sem arquivo .rds: {missing_tables}")
        self.ctx.write_text(ws.estado / "04_relatorio_analise.md", self._report(res, qa))
        reprovadas = [f for f, r in qa.items() if not r["aprovado"]]
        return {"n_final": res.fluxo_amostral.n_final, "modelos": len(res.diagnosticos_modelos),
                "tabelas": res.tabelas, "figuras": res.figuras, "figuras_reprovadas_qa": reprovadas}

    # ---------------------------------------------------------------- renv
    def _init_renv(self, cfg: dict) -> dict:
        rscript = shutil.which("Rscript")
        pkgs = ",".join(cfg.get("r_packages", []))
        proc = subprocess.run([rscript, "--vanilla", str(SCRIPTS_R_DIR / "init_renv.R"), str(self.ctx.ws.analise), pkgs],
                              capture_output=True, text=True, timeout=3600)
        status_path = self.ctx.ws.analise / "renv_status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {"erro": proc.stderr[-1000:]}
        self.ctx.log(f"  • renv: {len(status.get('instalados', []))} pacotes disponíveis, "
                     f"faltantes: {status.get('faltantes', [])}")
        return status

    # ---------------------------------------------------------------- geração/correção de scripts
    async def _write_script(self, name: str, purpose: str, base_ctx: str, written: dict[str, str]) -> str:
        previous = "\n\n".join(f"# ===== {n} =====\n{c}" for n, c in written.items())
        user = (f"{base_ctx}\n\nSCRIPTS JÁ EXECUTADOS COM SUCESSO:\n{previous[-20000:] or '(nenhum)'}\n\n"
                f"ESCREVA AGORA: 02_analise/{name}\nFINALIDADE: {purpose}\n"
                "Responda apenas com um bloco ```r``` contendo o script completo e executável.")
        text = await self.ctx.llm.complete(system=self.ctx.prompt("04_r_biostatistician"), user=user,
                                           phase=self.phase, role="local", sensitive=True)
        code = extract_code_block(text, "r")
        violations = static_check(code)
        if violations:
            text = await self.ctx.llm.complete(
                system=self.ctx.prompt("04_r_biostatistician"),
                user=("O script abaixo viola as regras de privacidade do sandbox:\n" +
                      "\n".join(f"- {v}" for v in violations) + f"\n\n```r\n{code}```\nCorrija e devolva o script completo."),
                phase=self.phase, role="local", sensitive=True)
            code = extract_code_block(text, "r")
        return code

    def _fixer(self, name: str, purpose: str, base_ctx: str):
        async def fix(code: str, feedback: str, attempt: int) -> str:
            self.ctx.log(f"  ↻ auto-correção {attempt}/{self.ctx.budget.limit('r_script')} de {name}")
            user = (f"{base_ctx[:15000]}\n\nSCRIPT {name} (finalidade: {purpose}):\n```r\n{code}```\n\n"
                    f"RESULTADO DA EXECUÇÃO (logs já sanitizados):\n{feedback}\n\n"
                    "Diagnostique a causa raiz e devolva o script COMPLETO corrigido em um bloco ```r```.")
            text = await self.ctx.llm.complete(system=self.ctx.prompt("04_r_biostatistician"), user=user,
                                               phase=self.phase, role="local", sensitive=True)
            return extract_code_block(text, "r")
        return fix

    def _post_validator(self, name: str):
        ws = self.ctx.ws

        def check() -> Optional[str]:
            data = json.loads(ws.resultados.read_text()) if ws.resultados.exists() else {}
            if name == "01_prep_dados.R":
                missing = [k for k in ("fluxo_amostral", "tratamento_missing") if k not in data]
                if missing:
                    return f"Chaves ausentes após 01_prep_dados.R: {missing}"
                if not (ws.analise / "intermediarios" / "analitica.rds").exists():
                    return "intermediarios/analitica.rds não foi salvo"
            if name == "02_modelos_principais.R":
                missing = [k for k in ("diagnosticos_modelos", "resultados_principais") if not data.get(k)]
                if missing:
                    return f"Chaves ausentes após 02_modelos_principais.R: {missing}"
                bad = [d.get("modelo") for d in data["diagnosticos_modelos"] if not d.get("converged")]
                if bad:
                    return f"Modelos não convergentes: {bad}. Reespecifique (escala, referência, família, otimizador)."
            if name == "04_tabelas_figuras.R":
                if not list(ws.tabelas.glob("*.rds")):
                    return "Nenhuma tabela .rds foi gerada em 03_entrega/tabelas"
                if not list(ws.figuras.glob("*.png")):
                    return "Nenhuma figura .png foi gerada em 03_entrega/figuras"
            return None
        return check

    # ---------------------------------------------------------------- QA visual
    async def _figure_qa(self, sandbox: RSandbox, base_ctx: str) -> dict[str, dict]:
        ws = self.ctx.ws
        min_dpi = int(self.ctx.settings.get("analysis", {}).get("figure_min_dpi", 300))
        results: dict[str, dict] = {}
        for png in sorted(ws.figuras.glob("*.png")):
            cached = self.ctx.state.get_step(self.phase, f"qa:{png.name}")
            if cached is not None:
                results[png.name] = cached
                continue
            qa = await inspect_figure(png, self.ctx.llm, phase=self.phase, min_dpi=min_dpi)
            while not qa.aprovado:
                try:
                    cycle = self.ctx.budget.consume_attempt("figure_qa", png.name)
                except AttemptLimitReached:
                    self.ctx.log(f"  ⚠ {png.name}: QA visual esgotou os ciclos; segue com ressalvas")
                    break
                self.ctx.log(f"  ↻ QA visual {png.name} ciclo {cycle}: {qa.problemas[:2]}")
                script = ws.analise / "04_tabelas_figuras.R"
                text = await self.ctx.llm.complete(
                    system=self.ctx.prompt("04_r_biostatistician"),
                    user=(f"Regenere APENAS a figura '{png.stem}' corrigindo cirurgicamente:\n"
                          + "\n".join(f"- {i}" for i in qa.instrucoes_correcao)
                          + f"\n\nScript atual:\n```r\n{script.read_text(encoding='utf-8')}```\n"
                          "Devolva o script 04_tabelas_figuras.R completo (as demais saídas devem permanecer idênticas)."),
                    phase=self.phase, role="local", sensitive=True)
                script.write_text(extract_code_block(text, "r"), encoding="utf-8")
                run = await sandbox.run_with_autofix(script, self._fixer("04_tabelas_figuras.R", "regenerar figura", base_ctx),
                                                     budget=self.ctx.budget)
                if not run.ok:
                    self.ctx.log(f"  ⚠ regeneração de {png.name} falhou: {run.feedback()[-300:]}")
                    break
                qa = await inspect_figure(png, self.ctx.llm, phase=self.phase, min_dpi=min_dpi)
            results[png.name] = qa.model_dump()
            self.ctx.state.save_step(self.phase, f"qa:{png.name}", results[png.name])
        return results

    def _report(self, res: ResultadosVerificados, qa: dict[str, dict]) -> str:
        f = res.fluxo_amostral
        lines = ["# Relatório da Fase 4 — Diagnósticos para o Portão Humano #2", "",
                 "## Fluxo amostral", f"- N inicial: {f.n_inicial}"]
        lines += [f"- Excluídos ({e.criterio}): {e.n_removido}" + (f" — {e.justificativa}" if e.justificativa else "")
                  for e in f.exclusoes]
        lines += [f"- **N analítico: {f.n_final}**", "", "## Dados faltantes", f"Método geral: {res.tratamento_missing.metodo_geral}"]
        lines += [f"- {v.variavel}: {v.pct_missing}% ({v.metodo})" for v in res.tratamento_missing.variaveis]
        lines += ["", "## Diagnósticos dos modelos", "| Modelo | Convergiu | Família | Dispersão | AIC | AUC | Avisos |",
                  "|---|---|---|---|---|---|---|"]
        for d in res.diagnosticos_modelos:
            lines.append(f"| {d.modelo} | {'sim' if d.converged else 'NÃO'} | {d.familia or '—'} | "
                         f"{'' if d.dispersao is None else round(d.dispersao, 3)} | {'' if d.aic is None else round(d.aic, 1)} | "
                         f"{'' if d.auc is None else round(d.auc, 3)} | {'; '.join(d.avisos) or '—'} |")
        lines += ["", "## Resultados principais", "| Modelo | Termo | Medida | Estimativa (IC 95%) | p |", "|---|---|---|---|---|"]
        for e in res.resultados_principais + res.resultados_sensibilidade:
            ic = f"{e.ic95_inf:.3f}–{e.ic95_sup:.3f}" if e.ic95_inf is not None and e.ic95_sup is not None else "—"
            p = "—" if e.p_valor is None else (f"{e.p_valor:.3g}")
            lines.append(f"| {e.modelo} | {e.termo} | {e.medida} | {e.estimativa:.3f} ({ic}) | {p} |")
        lines += ["", "## QA visual das figuras"]
        for name, r in qa.items():
            lines.append(f"- {name}: {'aprovada' if r['aprovado'] else 'COM RESSALVAS — ' + '; '.join(r['problemas'])}")
        return "\n".join(lines) + "\n"


__all__ = ["Phase4Analyst", "validate_results"]
