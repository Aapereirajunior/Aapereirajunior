"""Fase 6 — Redação Quarto encadeada + Auditorias + Humanizador + Revisor #2 + Renderização + Kit.

Ordem estratégica de redação (cada seção é checkpoint individual no SQLite):
  Metodos → Resultados → Discussao → Conclusao → Introducao → Titulo_Resumo → Material_Suplementar

Para cada seção: Redator (06) → Humanizador (06b) → varredura determinística de clichês.
Depois: auditorias (numérica vs JSON, citações vs .bib + Evidence Ledger, anti-plágio vs corpus,
independência vs rascunho), uma rodada de correção dirigida, até 2 rodadas de Revisor #2,
montagem do .qmd (tabelas via readRDS), renderização e Kit de Submissão → PORTÃO HUMANO #3.

A checagem de independência textual compara o texto final com o rascunho original usando somente os
hashes de n-gramas gravados pela Fase 1: esta fase nunca reabre o rascunho, e o firewall do LLMClient
impede que trechos dele entrem em qualquer prompt.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ..core.budget_guard import AttemptLimitReached
from ..core.schemas import SECOES_ORDEM_REDACAO, AuditReport, EvidenceLedger, Storyboard
from ..tools import integrity_checks as ic
from ..tools.literature_swarm import filter_bib, parse_bib_keys
from ..tools.quarto_renderer import (build_qmd, collect_objects, ensure_csl, insert_objects, load_checklist,
                                     number_paragraphs, parse_journal_rules, render_checklist_markdown,
                                     render_project)
from .base import PhaseAgent, compact_json

SECTION_HEADINGS = {
    "pt": {"Introducao": "Introdução", "Metodos": "Métodos", "Resultados": "Resultados", "Discussao": "Discussão",
           "Conclusao": "Conclusão", "Material_Suplementar": "Material Suplementar"},
    "en": {"Introducao": "Introduction", "Metodos": "Methods", "Resultados": "Results", "Discussao": "Discussion",
           "Conclusao": "Conclusion", "Material_Suplementar": "Supplementary Material"},
}
ARTICLE_ORDER = ["Introducao", "Metodos", "Resultados", "Discussao", "Conclusao"]
NUMERIC_STRICT = {"Resultados", "Titulo_Resumo", "Discussao", "Conclusao", "Material_Suplementar"}


class Phase6Editorial(PhaseAgent):
    phase = "phase6"
    title = "Fase 6 — Redação Quarto, auditorias e Kit de Submissão"

    async def run(self) -> dict:
        ws = self.ctx.ws
        self.lang = self.ctx.language()
        self.lk = "pt" if self.lang.lower().startswith("pt") else "en"
        self.sb = Storyboard.model_validate(self.ctx.read_json(ws.storyboard))
        self.results = self.ctx.read_json(ws.resultados)
        self.ledger: EvidenceLedger = self.ctx.ledger()
        self.inv = self.ctx.inventario()
        self.tables, self.figures = collect_objects(ws.entrega)
        self.bib_text = ws.bib_pool.read_text(encoding="utf-8") if ws.bib_pool.exists() else ""
        self.bib_keys = parse_bib_keys(self.bib_text)
        integ = self.ctx.settings.get("integrity", {})

        sections: dict[str, str] = {}
        for sid in SECOES_ORDEM_REDACAO:
            sections[sid] = await self.cached_step(f"secao:{sid}", lambda sid=sid: self._write_section(sid, sections))
            sections[sid] = await self.cached_step(f"humanizado:{sid}", lambda sid=sid: self._humanize(sid, sections[sid]))
            self.ctx.log(f"  ✓ seção {sid} redigida e humanizada")

        # Auditoria + correção dirigida (uma passada)
        report = await self._audit(sections, integ)
        if report.erros and not self.ctx.state.step_done(self.phase, "correcao_auditoria"):
            for sid in {a.secao for a in report.erros if a.secao}:
                findings = [a for a in report.erros if a.secao == sid]
                sections[sid] = await self._revise(sid, sections[sid], [f"{a.tipo}: {a.mensagem} | trecho: {a.trecho}" for a in findings])
                self.ctx.state.save_step(self.phase, f"humanizado:{sid}", sections[sid])
            self.ctx.state.save_step(self.phase, "correcao_auditoria", True)

        # Revisor #2 — no máximo `reviewer_max_rounds` rodadas
        rodada = 0
        while True:
            key = f"revisor2_rodada_{rodada + 1}"
            if not self.ctx.state.step_done(self.phase, key):
                try:
                    self.ctx.budget.consume_attempt("reviewer", "phase6")
                except AttemptLimitReached:
                    break
            rodada += 1
            parecer = await self.cached_step(key, lambda: self._review(sections))
            criticas = [c for c in parecer.get("criticas", []) if c.get("secao") in sections]
            if parecer.get("aprovado") and not criticas:
                break
            for sid in {c["secao"] for c in criticas}:
                pedidos = [f"{c.get('problema')} → {c.get('sugestao')}" for c in criticas if c["secao"] == sid]
                sections[sid] = await self._revise(sid, sections[sid], pedidos)
                self.ctx.state.save_step(self.phase, f"humanizado:{sid}", sections[sid])
            if self.ctx.budget.remaining("reviewer", "phase6") == 0:
                break

        final_report = await self._audit(sections, integ)
        qmd_paths = self._assemble(sections, final_report)
        kit = await self._submission_kit(qmd_paths["artigo"], final_report)
        formats = self.ctx.settings.get("render", {}).get("formats", ["docx", "pdf"])
        renders = render_project(ws.entrega, formats)
        self.ctx.write_text(ws.entrega / "relatorio_auditoria_final.md",
                            ic.render_audit_markdown(final_report) + "\n## Renderização\n\n" +
                            "\n".join(f"- {doc} [{fmt}]: {out}" for doc, r in renders.items() for fmt, out in r.items()) + "\n", encoding="utf-8")
        return {"qmd": {k: str(v) for k, v in qmd_paths.items()}, "renderizacao": renders, "kit": kit,
                "erros_auditoria": len(final_report.erros), "metricas": {k: v for k, v in final_report.metricas.items()
                                                                       if not k.startswith("_")}}

    # ---------------------------------------------------------------- redação
    def _ledger_for(self, keys: set[str]) -> list[dict]:
        by_key = self.ledger.by_key()
        return [by_key[k].model_dump() for k in sorted(keys) if k in by_key and by_key[k].status_retratacao != "retratado"]

    async def _write_section(self, sid: str, done: dict[str, str]) -> str:
        plan = self.sb.secao(sid)
        keys = {k for p in plan.paragrafos for k in p.citekeys}
        objects = {k: v["caption"] for k, v in {**self.tables, **self.figures}.items()}
        prior = "\n\n".join(f"## {k}\n{v}" for k, v in done.items() if k != sid)
        allowed_keys = sorted(e.citekey for e in self.ledger.entradas if e.status_retratacao != "retratado")
        user = (
            f"IDIOMA: {self.lang}\nSEÇÃO A REDIGIR: {sid} ({plan.titulo}) — destino {plan.destino}, "
            f"limite {plan.limite_palavras} palavras.\n\nFIO CONDUTOR DO ARTIGO: {self.sb.fio_condutor}\n\n"
            f"PLANO PARÁGRAFO A PARÁGRAFO:\n{compact_json([p.model_dump() for p in plan.paragrafos], 12000)}\n\n"
            f"RESULTADOS VERIFICADOS (única fonte de números):\n{compact_json(self.results, 20000)}\n\n"
            f"FICHAMENTOS DAS CITAÇÕES PLANEJADAS:\n{compact_json(self._ledger_for(keys), 15000)}\n\n"
            f"CITEKEYS PERMITIDAS: {allowed_keys}\n\nTABELAS/FIGURAS (labels → legenda): "
            f"{json.dumps(objects, ensure_ascii=False)}\n\nGLOSSÁRIO OBRIGATÓRIO: "
            f"{json.dumps(self.inv.glossario_obrigatorio, ensure_ascii=False)}\n"
            f"CONTEXTO TÁCITO LOCAL: {json.dumps(self.inv.contexto_tacito_local, ensure_ascii=False)}\n\n"
            f"SEÇÕES JÁ REDIGIDAS (mantenha alinhamento absoluto de números e termos):\n{prior[-30000:] or '(nenhuma)'}\n"
        )
        if sid == "Titulo_Resumo":
            rules = parse_journal_rules(self.ctx.journal_rules_text())
            user += (f"\nESTRUTURA DO RESUMO: {self.sb.estrutura_resumo or rules.estrutura_resumo}; "
                     f"limite do resumo: {rules.limite_resumo or 250} palavras.\nResponda APENAS em JSON: "
                     '{"titulo": "...", "resumo": "...", "palavras_chave": ["..."]}')
            data = await self.ctx.llm.complete_json(system=self.ctx.prompt("06_quarto_scientific_writer"), user=user,
                                                    phase=self.phase, role="writer")
            return json.dumps(data, ensure_ascii=False)
        return await self.ctx.llm.complete(system=self.ctx.prompt("06_quarto_scientific_writer"), user=user,
                                           phase=self.phase, role="writer")

    async def _humanize(self, sid: str, text: str) -> str:
        if sid == "Titulo_Resumo":
            data = json.loads(text)
            data["resumo"] = await self._humanize_text(data.get("resumo", ""), "Resumo")
            return json.dumps(data, ensure_ascii=False)
        return await self._humanize_text(text, sid)

    async def _humanize_text(self, text: str, sid: str) -> str:
        user = (f"IDIOMA: {self.lang}\nSEÇÃO: {sid}\nGLOSSÁRIO (não alterar): "
                f"{json.dumps(self.inv.glossario_obrigatorio, ensure_ascii=False)}\n\nTEXTO:\n{text}")
        out = await self.ctx.llm.complete(system=self.ctx.prompt("06b_humanizer_editor"), user=user,
                                          phase=self.phase, role="writer", temperature=0.4)
        out = _strip_fences(out)
        if not _same_anchors(text, out):
            self.ctx.log(f"  ⚠ Humanizador alterou números/citações/marcadores em {sid}; mantendo versão do Redator")
            out = text
        if ic.cliche_scan(out).metricas.get("cliches_detectados", 0):
            out = ic.remove_cliches(out)
        return out

    async def _revise(self, sid: str, text: str, requests: list[str]) -> str:
        user = (f"IDIOMA: {self.lang}\nREVISE a seção {sid} atendendo SOMENTE aos pontos abaixo, sem inventar números "
                f"ou citações:\n- " + "\n- ".join(requests[:40]) +
                f"\n\nRESULTADOS VERIFICADOS:\n{compact_json(self.results, 15000)}\n\nCITEKEYS PERMITIDAS: "
                f"{sorted(self.bib_keys)}\n\nTEXTO ATUAL:\n{text}\n\n"
                + ("Responda no mesmo formato JSON {titulo, resumo, palavras_chave}." if sid == "Titulo_Resumo"
                   else "Devolva a seção completa revisada em Markdown."))
        if sid == "Titulo_Resumo":
            data = await self.ctx.llm.complete_json(system=self.ctx.prompt("06_quarto_scientific_writer"),
                                                    user=user, phase=self.phase, role="writer")
            return json.dumps(data, ensure_ascii=False)
        out = await self.ctx.llm.complete(system=self.ctx.prompt("06_quarto_scientific_writer"), user=user,
                                          phase=self.phase, role="writer")
        out = _strip_fences(out)
        return ic.remove_cliches(out) if ic.cliche_scan(out).metricas.get("cliches_detectados") else out

    async def _review(self, sections: dict[str, str]) -> dict:
        manuscript = "\n\n".join(f"## [{sid}]\n{txt}" for sid, txt in sections.items())
        user = (f"MODO MANUSCRITO. Avalie o manuscrito completo abaixo (seções marcadas entre colchetes).\n"
                f"RESULTADOS VERIFICADOS:\n{compact_json(self.results, 12000)}\n\nPROTOCOLO:\n"
                f"{self.ctx.ws.protocolo.read_text(encoding='utf-8')[:10000]}\n\nMANUSCRITO:\n{manuscript[:60000]}\n\n"
                'Responda JSON: {"aprovado": bool, "criticas": [{"secao": "<id da seção>", "gravidade": '
                '"critica|maior|menor", "problema": "...", "sugestao": "..."}], "comentario_geral": "..."}')
        data = await self.ctx.llm.complete_json(system=self.ctx.prompt("03b_reviewer2_redteam"), user=user,
                                                phase=self.phase, role="reviewer")
        if not isinstance(data, dict):
            return {"aprovado": False, "criticas": []}
        data["criticas"] = [c for c in data.get("criticas", []) if isinstance(c, dict) and c.get("gravidade") != "menor"]
        return data

    # ---------------------------------------------------------------- auditoria
    async def _judge(self, sentence: str, entry: dict) -> bool:
        user = (f"MODO JUIZ.\nFRASE: {sentence}\nFICHAMENTO: {json.dumps(entry, ensure_ascii=False)}\n"
                'Responda JSON {"suportada": true|false, "motivo": "..."}')
        try:
            data = await self.ctx.llm.complete_json(system=self.ctx.prompt("02b_evidence_extractor"), user=user,
                                                    phase=self.phase, role="fast")
            return bool(data.get("suportada"))
        except Exception:  # noqa: BLE001 — na dúvida, sinaliza
            return False

    async def _audit(self, sections: dict[str, str], integ: dict) -> AuditReport:
        report = AuditReport()
        tol = float(integ.get("numeric_relative_tolerance", 0.005))
        for sid, text in sections.items():
            body = _section_text(sid, text)
            report.extend(ic.numeric_audit(body, self.results, self.lang, tol,
                                           severity="erro" if sid in NUMERIC_STRICT else "alerta", secao=sid))
            cit = await ic.citation_audit(body, self.bib_keys, self.ledger,
                                          float(integ.get("citation_support_min_score", 0.12)), judge=self._judge)
            for a in cit.achados:
                a.secao = sid
            report.achados.extend(cit.achados)
        full = "\n\n".join(_section_text(s, t) for s, t in sections.items())
        corpus = []
        if self.ctx.ws.corpus.exists():
            corpus = [json.loads(line) for line in self.ctx.ws.corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
        report.extend(ic.plagiarism_check(full, corpus, int(integ.get("plagiarism_ngram_n", 6)),
                                          float(integ.get("plagiarism_containment_alert", 0.35))))
        # Independência vs rascunho: usa apenas hashes de n-gramas gravados pela Fase 1 (o rascunho não é reaberto).
        fw = ic.DraftFirewall.load(self.ctx.ws.firewall_fingerprint)
        if fw is not None and fw.audit_hashes:
            report.extend(ic.draft_independence_hashed(full, fw.audit_hashes, fw.audit_n,
                                                       float(integ.get("draft_recycled_sentence_containment", 0.5)),
                                                       float(integ.get("draft_max_recycled_ratio", 0.10))))
        report.extend(ic.cliche_scan(full))
        report.extend(ic.glossary_check(full, self.inv.glossario_obrigatorio))
        report.extend(ic.retraction_report(self.ledger))
        return report

    # ---------------------------------------------------------------- montagem
    def _assemble(self, sections: dict[str, str], report: AuditReport) -> dict[str, Path]:
        ws = self.ctx.ws
        meta = json.loads(sections["Titulo_Resumo"])
        cited = set(CITE_KEY_RE.findall("\n".join(sections.values()))) & self.bib_keys
        ws.entrega.mkdir(parents=True, exist_ok=True)
        (ws.entrega / "referencias.bib").write_text(filter_bib(self.bib_text, cited), encoding="utf-8")
        rules = parse_journal_rules(self.ctx.journal_rules_text())
        style = rules.estilo_csl or self.ctx.settings.get("csl_padrao", "vancouver")
        try:
            ensure_csl(style, ws.entrega)
        except Exception as exc:  # noqa: BLE001
            self.ctx.log(f"  ⚠ CSL '{style}' indisponível ({exc}); usando Vancouver")
            ensure_csl("vancouver", ws.entrega)
        heads = SECTION_HEADINGS[self.lk]
        formats = self.ctx.settings.get("render", {}).get("formats", ["docx", "pdf"])
        ref_doc = self.ctx.settings.get("render", {}).get("reference_doc")
        sup_tables = {k: v for k, v in self.tables.items()}
        art_sections = [(heads[s], insert_objects(sections[s], self.tables, self.figures)) for s in ARTICLE_ORDER]
        article = build_qmd(title=meta.get("titulo") or self.sb.titulo_provisorio, sections=art_sections,
                            lang=self.lang, abstract=meta.get("resumo"), keywords=meta.get("palavras_chave"),
                            reference_doc=ref_doc, formats=formats)
        sup = build_qmd(title=f"{heads['Material_Suplementar']} — {meta.get('titulo') or self.sb.titulo_provisorio}",
                        sections=[("", insert_objects(sections["Material_Suplementar"], sup_tables, self.figures))],
                        lang=self.lang, reference_doc=ref_doc, formats=formats,
                        extra_meta={"crossref": {"fig-prefix": "Figura S" if self.lk == "pt" else "Figure S",
                                                 "tbl-prefix": "Tabela S" if self.lk == "pt" else "Table S"}})
        paths = {"artigo": ws.entrega / "artigo_final.qmd", "suplementar": ws.entrega / "material_suplementar.qmd"}
        paths["artigo"].write_text(article, encoding="utf-8")
        paths["suplementar"].write_text(sup, encoding="utf-8")
        refs = ic.figure_ref_check(article, set(self.tables), set(self.figures))
        report.extend(refs)
        return paths

    async def _submission_kit(self, article_path: Path, report: AuditReport) -> dict[str, str]:
        ws = self.ctx.ws
        crit = self.ctx.read_json(ws.critica_revisor) if ws.critica_revisor.exists() else {}
        checklist_name = crit.get("checklist", "STROBE")
        items = load_checklist(checklist_name)
        if checklist_name in ("RECORD", "RECORD_PE"):
            items = load_checklist("STROBE") + items
            if checklist_name == "RECORD_PE":
                items = items + [i for i in load_checklist("RECORD") if i not in items]
        paragraphs = number_paragraphs(article_path.read_text(encoding="utf-8"))
        para_txt = "\n".join(f"[{p['id']}] {p['texto'][:400]}" for p in paragraphs)
        mapping = await self.cached_step("kit_checklist", lambda: self.ctx.llm.complete_json(
            system=self.ctx.prompt("06c_submission_kit"),
            user=(f"MODO CHECKLIST ({checklist_name}).\nITENS:\n{json.dumps(items, ensure_ascii=False)}\n\n"
                  f"PARÁGRAFOS NUMERADOS DO ARTIGO:\n{para_txt[:60000]}\n\n"
                  'Responda JSON {"itens": [{"item": "...", "local": "<id exato do parágrafo ou \'não se aplica\'>", '
                  '"status": "atendido|parcial|ausente|nao_se_aplica"}]}'),
            phase=self.phase, role="fast"))
        valid_ids = {p["id"] for p in paragraphs}
        by_item = {m.get("item"): m for m in (mapping.get("itens", []) if isinstance(mapping, dict) else [])}
        filled = []
        for it in items:
            m = by_item.get(it["item"], {})
            locs = [loc.strip() for loc in str(m.get("local") or "").split(";") if loc.strip()]
            locs_ok = [loc for loc in locs if loc in valid_ids or _is_not_applicable(loc)]
            status = m.get("status", "ausente")
            if locs and not locs_ok:
                status = "verificar (local inválido)"
            filled.append({**it, "local": "; ".join(locs_ok) or "—", "status": status})
        self.ctx.write_text(ws.entrega / "checklist_preenchido.md", render_checklist_markdown(checklist_name, filled), encoding="utf-8")

        meta = json.loads((self.ctx.state.get_step(self.phase, "humanizado:Titulo_Resumo")) or "{}")
        rules = parse_journal_rules(self.ctx.journal_rules_text())
        letter = await self.cached_step("kit_cover_letter", lambda: self.ctx.llm.complete(
            system=self.ctx.prompt("06c_submission_kit"),
            user=(f"MODO CARTA. Idioma do periódico: {self.lang}. Periódico: {rules.nome or 'periódico alvo'}.\n"
                  f"Título: {meta.get('titulo')}\nResumo: {meta.get('resumo')}\n"
                  f"Desenho e checklist: {checklist_name}\nResultados principais: "
                  f"{compact_json(self.results.get('resultados_principais', []), 4000)}\n"
                  "Redija a carta de apresentação ao editor em Markdown."),
            phase=self.phase, role="writer"))
        self.ctx.write_text(ws.entrega / "cover_letter.md", _strip_fences(letter), encoding="utf-8")
        return {"checklist": str(ws.entrega / "checklist_preenchido.md"), "cover_letter": str(ws.entrega / "cover_letter.md"),
                "auditoria": str(ws.entrega / "relatorio_auditoria_final.md")}


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", t, flags=re.S)
    return m.group(1).strip() if m else t


# Citekey Pandoc: começa e termina em caractere de palavra; pontuação interna (:.#$%&+?<>~/-) é permitida,
# mas a pontuação final colada em citações narrativas ("@silva2020.", "@silva2020,") não faz parte da chave.
CITE_KEY_RE = re.compile(r"(?<![\w.@])@([A-Za-z0-9_](?:[\w:.#$%&+?<>~/-]*\w)?)")
NOT_APPLICABLE = ("não se aplica", "nao se aplica", "not applicable", "n/a", "na")


def _is_not_applicable(loc: str) -> bool:
    low = loc.strip().lower().rstrip(".")
    return any(low == na or low.startswith(na + " ") or low.startswith(na + "(") for na in NOT_APPLICABLE)


def _anchors(text: str) -> tuple[Counter, Counter, Counter]:
    """Números, citações e marcadores com multiplicidade (um número repetido conta duas vezes)."""
    cites = Counter(CITE_KEY_RE.findall(text))
    without_refs = CITE_KEY_RE.sub(" ", ic.strip_citations(text))
    marks = Counter(re.sub(r"\s+", "", m) for m in re.findall(r"\{\{\s*(?:tbl|fig)-[\w-]+\s*\}\}", text))
    without_marks = re.sub(r"\{\{\s*(?:tbl|fig)-[\w-]+\s*\}\}", " ", without_refs)
    nums = Counter(re.findall(r"\d+(?:[.,]\d+)?", without_marks))
    return nums, cites, marks


def _same_anchors(before: str, after: str) -> bool:
    """O Humanizador só pode mexer no estilo: números, citações e marcadores devem ser idênticos,
    inclusive na quantidade de ocorrências (remover um IC 95% ou um p-valor invalida a versão)."""
    return _anchors(before) == _anchors(after)


def _section_text(sid: str, text: str) -> str:
    if sid == "Titulo_Resumo":
        try:
            data = json.loads(text)
            return f"{data.get('titulo', '')}. {data.get('resumo', '')}"
        except json.JSONDecodeError:
            return text
    return text


__all__ = ["Phase6Editorial"]
