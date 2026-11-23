"""Servidor MCP local do SciForge — ferramentas para IDEs (Claude Code, Cursor, VS Code) e para o Goose.

Transporte STDIO limpo: o stdout pertence exclusivamente ao protocolo JSON-RPC. Todo logging vai para
stderr, e cada ferramenta roda com `sys.stdout` redirecionado para stderr (`stdout_guard`), de modo que
um `print()` esquecido em qualquer biblioteca não corrompe a conexão. Subprocessos (R, Quarto, Pandoc)
têm a saída capturada por `platform_utils.run_command` e nunca escrevem no descritor 1.

Ferramentas utilitárias:
  search_literature_swarm, profile_r_dataset, verify_doi_and_retraction,
  audit_quarto_manuscript, render_quarto_submission
Ferramentas de operação do fluxo (usadas pelas receitas em goose_recipes/):
  sciforge_init_project, sciforge_status, sciforge_run_phase, sciforge_approve_gate,
  sciforge_read_artifact, sciforge_update_protocol

Firewall anti-retalhos: nenhuma ferramenta devolve o conteúdo de `00_semente/rascunho.*` ou de
`graficos_preliminares/`. A Fase 1 lê o rascunho DENTRO do processo do SciForge; o agente (Goose)
recebe apenas o inventário de átomos conceituais.

Execução: `sciforge-mcp` (stdio) ou `python -m sciforge.mcp_server.server`.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Optional

try:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # SDK MCP 2.x renomeou FastMCP para MCPServer (mesma API de decorators)
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

from ..core.config import TEMPLATE_DIR, Workspace, load_settings
from ..core.schemas import EvidenceLedger
from ..core.state_manager import GATES, StateManager
from ..tools import integrity_checks as ic
from ..tools.data_profiler import profile_dataset
from ..tools.literature_swarm import LiteratureSwarm, SwarmConfig, parse_bib_keys, rank_papers
from ..tools.quarto_renderer import collect_objects, ensure_csl, render_project

logger = logging.getLogger("sciforge.mcp")


def configure_stderr_logging(level: int = logging.INFO) -> None:
    """Todo log (inclusive de bibliotecas como httpx) vai para stderr; nada para stdout."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "stream", None) is sys.stdout or getattr(handler, "stream", None) is sys.__stdout__:
            root.removeHandler(handler)
    if not any(getattr(h, "stream", None) is sys.stderr for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)


configure_stderr_logging()


def stdout_guard(fn: Callable) -> Callable:
    """Redireciona `sys.stdout` para stderr enquanto a ferramenta executa (sync ou async)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with contextlib.redirect_stdout(sys.stderr):
                return await fn(*args, **kwargs)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*args, **kwargs)
    return sync_wrapper


def _stderr_log(message: str) -> None:
    logger.info(message)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


mcp = FastMCP("sciforge")


def _swarm_config() -> SwarmConfig:
    """Respeita settings.yaml (rate limits, bulk) e o .env (POLITE_EMAIL, SEMANTIC_SCHOLAR_API_KEY)."""
    return SwarmConfig.from_settings(load_settings())


# =============================================================================================
# Auditoria (compartilhada com a CLI)
# =============================================================================================

async def audit_manuscript(qmd_path: Path, results_json_path: Path, evidence_ledger_path: Path,
                           draft_path: Optional[Path] = None, fingerprint_path: Optional[Path] = None,
                           bib_path: Optional[Path] = None, corpus_path: Optional[Path] = None,
                           lang: str = "pt-BR") -> str:
    """Executa as auditorias determinísticas e devolve o relatório em Markdown."""
    qmd_path = Path(qmd_path)
    text = qmd_path.read_text(encoding="utf-8")
    results = json.loads(Path(results_json_path).read_text(encoding="utf-8"))
    ledger = EvidenceLedger.model_validate(json.loads(Path(evidence_ledger_path).read_text(encoding="utf-8")))
    bib_path = Path(bib_path) if bib_path else qmd_path.with_name("referencias.bib")
    bib_keys = parse_bib_keys(bib_path.read_text(encoding="utf-8")) if bib_path.exists() else {e.citekey for e in ledger.entradas}
    report = ic.AuditReport()
    report.extend(ic.numeric_audit(text, results, lang))
    report.extend(await ic.citation_audit(text, bib_keys, ledger))
    if draft_path and Path(draft_path).exists():
        report.extend(ic.draft_independence(text, Path(draft_path).read_text(encoding="utf-8", errors="replace")))
    elif fingerprint_path:
        fw = ic.DraftFirewall.load(Path(fingerprint_path))
        if fw is not None and fw.audit_hashes:
            report.extend(ic.draft_independence_hashed(text, fw.audit_hashes, fw.audit_n))
    if corpus_path and Path(corpus_path).exists():
        corpus = [json.loads(line) for line in Path(corpus_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        report.extend(ic.plagiarism_check(text, corpus))
    report.extend(ic.cliche_scan(text))
    tables, figures = collect_objects(qmd_path.parent)
    report.extend(ic.figure_ref_check(text, set(tables), set(figures)))
    return ic.render_audit_markdown(report)


# =============================================================================================
# Ferramentas utilitárias
# =============================================================================================

@mcp.tool()
@stdout_guard
async def search_literature_swarm(query: str, years_back: int = 5, max_results: int = 20) -> str:
    """Busca paralela em OpenAlex, EuropePMC, Crossref e Semantic Scholar; retorna artigos deduplicados por DOI."""
    async with LiteratureSwarm(_swarm_config()) as swarm:
        papers, errors = await swarm.search_all(query, years_back=years_back, max_results=max_results)
    ranked = rank_papers(papers)[: max_results * 2]
    return _json({"total": len(papers), "erros": errors,
                  "artigos": [{"titulo": p.titulo, "doi": p.doi, "ano": p.ano, "revista": p.revista,
                               "autores": p.autores[:5], "citacoes": p.citacoes, "fontes": p.fontes,
                               "open_access": p.open_access, "retratado": p.retratado,
                               "resumo": (p.resumo or "")[:600]} for p in ranked]})


@mcp.tool()
@stdout_guard
def profile_r_dataset(file_path: str) -> str:
    """Raio-X agregado (somente leitura, LGPD) de .parquet/.csv/.rds/.fst via scripts_r/profile_data.R."""
    return profile_dataset(Path(file_path)).model_dump_json(indent=1)


@mcp.tool()
@stdout_guard
async def verify_doi_and_retraction(doi_or_title: str) -> str:
    """Confere existência de um DOI (ou título) no Crossref/OpenAlex e seu status de retratação/correção."""
    async with LiteratureSwarm(_swarm_config()) as swarm:
        info = await swarm.verify_reference(doi_or_title)
    return _json(info)


@mcp.tool()
@stdout_guard
async def audit_quarto_manuscript(qmd_path: str, results_json_path: str, evidence_ledger_path: str,
                                  draft_path: str = "") -> str:
    """Auditoria numérica, de citações (Evidence Ledger), clichês, cross-refs e independência vs rascunho.

    Sem `draft_path`, a independência textual usa apenas os hashes de n-gramas gravados pela Fase 1.
    """
    qmd = Path(qmd_path)
    root = qmd.parent.parent
    return await audit_manuscript(qmd, Path(results_json_path), Path(evidence_ledger_path),
                                  Path(draft_path) if draft_path else None,
                                  fingerprint_path=root / "01_estado" / ".firewall_fingerprint.json",
                                  corpus_path=root / "01_estado" / "02_corpus.jsonl")


@mcp.tool()
@stdout_guard
def render_quarto_submission(project_dir: str, csl_style: str = "vancouver", output_formats: str = "docx,pdf") -> str:
    """Garante o .csl escolhido e renderiza artigo_final.qmd + material_suplementar.qmd (Quarto)."""
    entrega = Path(project_dir) / "03_entrega"
    csl = ensure_csl(csl_style, entrega)
    res = render_project(entrega, [f.strip() for f in output_formats.split(",") if f.strip()])
    return _json({"csl": str(csl), "resultados": res})


# =============================================================================================
# Operação do fluxo (receitas Goose)
# =============================================================================================

ARTIFACTS: dict[str, tuple[str, str]] = {
    # nome: (caminho relativo ao projeto, descrição)
    "inventario": ("01_estado/01_inventario_semente.json", "átomos conceituais da semente (Fase 1)"),
    "estado_da_arte": ("01_estado/02_estado_da_arte.md", "síntese da literatura (Fase 2)"),
    "evidence_ledger": ("01_estado/02_evidence_ledger.json", "fichamentos de evidência (Fase 2)"),
    "protocolo": ("01_estado/03_protocolo_proposto.md", "protocolo epidemiológico (Fase 3)"),
    "critica_revisor": ("01_estado/03_critica_revisor2.json", "pareceres do Revisor #2 (Fase 3)"),
    "resultados": ("01_estado/04_resultados_verificados.json", "contrato de diagnósticos (Fase 4)"),
    "relatorio_analise": ("01_estado/04_relatorio_analise.md", "relatório para o Portão #2 (Fase 4)"),
    "storyboard": ("01_estado/05_storyboard.json", "estrutura narrativa (Fase 5)"),
    "artigo_qmd": ("03_entrega/artigo_final.qmd", "manuscrito Quarto (Fase 6)"),
    "suplementar_qmd": ("03_entrega/material_suplementar.qmd", "material suplementar (Fase 6)"),
    "auditoria": ("03_entrega/relatorio_auditoria_final.md", "relatório de auditoria final (Fase 6)"),
    "checklist": ("03_entrega/checklist_preenchido.md", "checklist de relato (Fase 6)"),
    "cover_letter": ("03_entrega/cover_letter.md", "carta ao editor (Fase 6)"),
    "regras_revista": ("00_semente/regras_revista.txt", "normas do periódico (fornecidas pelo autor)"),
}


def read_artifact(project_dir: Path, artifact: str, max_chars: int = 20000) -> str:
    """Lê somente artefatos da lista branca; o rascunho original nunca é exposto."""
    if artifact not in ARTIFACTS:
        raise ValueError(f"Artefato desconhecido '{artifact}'. Disponíveis: {', '.join(sorted(ARTIFACTS))}")
    ws = Workspace(Path(project_dir).resolve())
    path = (ws.root / ARTIFACTS[artifact][0]).resolve()
    if ic.is_seed_draft_path(path, ws.semente):
        raise ic.FirewallViolation("O rascunho original não pode ser lido fora da Fase 1.")
    if not path.exists():
        return f"(artefato '{artifact}' ainda não foi gerado — {ARTIFACTS[artifact][1]})"
    text = path.read_text(encoding="utf-8")
    return text if len(text) <= max_chars else text[:max_chars] + f"\n... [truncado; {len(text)} caracteres no total]"


@mcp.tool()
@stdout_guard
def sciforge_init_project(project_dir: str) -> str:
    """Cria um projeto SciForge (00_semente, 01_estado, 02_analise, 03_entrega) a partir do template."""
    target = Path(project_dir).resolve()
    if target.exists() and any(target.iterdir()):
        return _json({"criado": False, "motivo": f"{target} já existe e não está vazio."})
    shutil.copytree(TEMPLATE_DIR, target, dirs_exist_ok=True)
    for keep in target.rglob(".gitkeep"):
        keep.unlink()
    Workspace(target).ensure()
    StateManager(Workspace(target).db_path)
    return _json({"criado": True, "projeto": str(target),
                  "proximos_passos": ["Pesquisador: coloque rascunho.* em 00_semente/ (o agente NÃO deve lê-lo)",
                                      "Pesquisador: coloque os dados em 00_semente/dados/",
                                      "Pesquisador: edite 00_semente/regras_revista.txt",
                                      "Agente: execute a receita da Fase 1"]})


@mcp.tool()
@stdout_guard
def sciforge_status(project_dir: str) -> str:
    """Fases, portões humanos, custos por fase e a próxima ação recomendada para o projeto."""
    from ..orchestrator import project_status
    return _json(project_status(Path(project_dir)))


@mcp.tool()
@stdout_guard
async def sciforge_run_phase(project_dir: str, phase: str) -> str:
    """Executa uma fase (phase1..phase6). Recusa se fases anteriores ou portões humanos estiverem pendentes.

    Ao terminar uma fase com portão (3, 4, 6), devolve o resumo que o pesquisador precisa avaliar.
    """
    from ..orchestrator import PhaseOrderError, run_single_phase
    try:
        result = await run_single_phase(Path(project_dir), phase, log=_stderr_log)
    except PhaseOrderError as exc:
        return _json({"executada": False, "motivo": str(exc)})
    except Exception as exc:  # noqa: BLE001 — o estado já foi salvo; o agente relata e o humano decide
        logger.exception("Falha em %s", phase)
        return _json({"executada": False, "erro": f"{type(exc).__name__}: {exc}",
                      "dica": "O progresso foi salvo; corrija a causa e execute a mesma fase de novo para retomar."})
    return _json({"executada": True, **result})


@mcp.tool()
@stdout_guard
def sciforge_approve_gate(project_dir: str, gate: str, human_decision: str) -> str:
    """Registra a aprovação HUMANA de um portão (gate1|gate2|gate3).

    Só chame após o pesquisador escrever explicitamente que aprova; `human_decision` deve conter a
    frase dele (ex.: "Aprovo o protocolo com a alteração X").
    """
    if gate not in GATES:
        return _json({"aprovado": False, "motivo": f"Portão inválido: {gate} (use {', '.join(GATES)})"})
    if len(human_decision.strip()) < 8:
        return _json({"aprovado": False, "motivo": "Informe a decisão do pesquisador em human_decision."})
    ws = Workspace(Path(project_dir).resolve())
    st = StateManager(ws.db_path)
    phase = GATES[gate]
    if st.phase_status(phase) != "done":
        return _json({"aprovado": False, "motivo": f"{phase} ainda não foi concluída; não há o que aprovar."})
    st.set_gate(gate, "approved", f"via MCP: {human_decision.strip()[:500]}")
    return _json({"aprovado": True, "portao": gate})


@mcp.tool()
@stdout_guard
def sciforge_read_artifact(project_dir: str, artifact: str, max_chars: int = 20000) -> str:
    """Lê um artefato estruturado do projeto (lista branca). O rascunho original NUNCA é retornado.

    Artefatos: inventario, estado_da_arte, evidence_ledger, protocolo, critica_revisor, resultados,
    relatorio_analise, storyboard, artigo_qmd, suplementar_qmd, auditoria, checklist, cover_letter,
    regras_revista.
    """
    try:
        return read_artifact(Path(project_dir), artifact, max_chars)
    except (ValueError, ic.FirewallViolation) as exc:
        return _json({"erro": str(exc)})


@mcp.tool()
@stdout_guard
def sciforge_update_protocol(project_dir: str, protocol_markdown: str, human_request: str) -> str:
    """Substitui 01_estado/03_protocolo_proposto.md com edições pedidas pelo pesquisador (antes do Portão #1).

    O texto não pode conter trechos do rascunho original (firewall anti-retalhos).
    """
    ws = Workspace(Path(project_dir).resolve())
    st = StateManager(ws.db_path)
    if st.phase_status("phase3") != "done":
        return _json({"atualizado": False, "motivo": "A Fase 3 ainda não gerou o protocolo."})
    if st.gate_status("gate1") == "approved":
        return _json({"atualizado": False, "motivo": "Portão #1 já aprovado; use `sciforge reset <projeto> phase4` antes."})
    fw = ic.DraftFirewall.load(ws.firewall_fingerprint)
    if fw is not None:
        try:
            fw.guard("phase3", protocol_markdown)
        except ic.FirewallViolation as exc:
            return _json({"atualizado": False, "motivo": str(exc)})
    ws.protocolo.write_text(protocol_markdown, encoding="utf-8")
    st.log(f"Protocolo editado via MCP: {human_request[:300]}", phase="phase3")
    return _json({"atualizado": True, "caminho": str(ws.protocolo)})


def main() -> None:
    configure_stderr_logging()
    mcp.run()


if __name__ == "__main__":
    main()
