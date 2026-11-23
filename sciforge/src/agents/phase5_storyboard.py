"""Fase 5 — Arquiteto Narrativo: storyboard editorial do zero.

Define o fio condutor parágrafo a parágrafo, respeitando os limites de `regras_revista.txt`, e
separa Manuscrito Principal vs. Material Suplementar. Valida que toda tabela/figura/citekey/resultado
referenciado existe de fato (Fase 4 e Evidence Ledger).
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from ..core.schemas import Storyboard
from ..tools.quarto_renderer import collect_objects, parse_journal_rules
from .base import GateBlocked, PhaseAgent, compact_json


def validate_storyboard(sb: Storyboard, tables: set[str], figures: set[str], citekeys: set[str],
                        results: dict) -> list[str]:
    problems = []
    for s in sb.secoes:
        for i, p in enumerate(s.paragrafos):
            loc = f"{s.id} §{i + 1}"
            for t in p.tabelas:
                if t not in tables:
                    problems.append(f"{loc}: tabela inexistente {t}")
            for f in p.figuras:
                if f not in figures:
                    problems.append(f"{loc}: figura inexistente {f}")
            for k in p.citekeys:
                if k not in citekeys:
                    problems.append(f"{loc}: citekey fora do Evidence Ledger @{k}")
            for ref in p.resultados_refs:
                root = ref.split("[")[0].split(".")[0]
                if root not in results:
                    problems.append(f"{loc}: referência de resultado inexistente {ref}")
    return problems


class Phase5Storyboard(PhaseAgent):
    phase = "phase5"
    title = "Fase 5 — Storyboard narrativo (Manuscrito vs. Suplementar)"

    async def run(self) -> dict:
        ws = self.ctx.ws
        inv = self.ctx.inventario()
        ledger = self.ctx.ledger()
        results = json.loads(ws.resultados.read_text(encoding="utf-8"))
        tables, figures = collect_objects(ws.entrega)
        rules_text = self.ctx.journal_rules_text()
        rules = parse_journal_rules(rules_text)
        ledger_brief = [{"citekey": e.citekey, "ano": e.ano, "desenho": e.desenho,
                         "achados": e.achados_principais[:3]} for e in ledger.entradas
                        if e.status_retratacao != "retratado"]
        user = (
            f"PROTOCOLO:\n{ws.protocolo.read_text(encoding='utf-8')[:15000]}\n\n"
            f"RESULTADOS VERIFICADOS (agregados):\n{compact_json(results, 20000)}\n\n"
            f"TABELAS DISPONÍVEIS: {json.dumps({k: v['caption'] for k, v in tables.items()}, ensure_ascii=False)}\n"
            f"FIGURAS DISPONÍVEIS: {json.dumps({k: v['caption'] for k, v in figures.items()}, ensure_ascii=False)}\n\n"
            f"EVIDENCE LEDGER (resumo):\n{compact_json(ledger_brief, 20000)}\n\n"
            f"ESTADO DA ARTE:\n{ws.estado_da_arte.read_text(encoding='utf-8')[:12000] if ws.estado_da_arte.exists() else ''}\n\n"
            f"REGRAS DA REVISTA:\n{rules_text[:4000]}\nLimite de palavras detectado: {rules.limite_palavras}; "
            f"estrutura do resumo: {rules.estrutura_resumo}; máx. tabelas+figuras: {rules.max_tabelas_figuras}\n\n"
            f"PREFERÊNCIAS DO AUTOR: {json.dumps(inv.restricoes_e_preferencias_autor, ensure_ascii=False)}\n"
            "Desenhe o storyboard no contrato JSON."
        )
        attempt_feedback = ""
        for attempt in range(3):
            data = await self.ctx.llm.complete_json(system=self.ctx.prompt("05_narrative_storyboarder"),
                                                    user=user + attempt_feedback, phase=self.phase, role="default")
            try:
                if rules.limite_palavras and not data.get("limite_palavras_total"):
                    data["limite_palavras_total"] = rules.limite_palavras
                sb = Storyboard.model_validate(data)
            except ValidationError as exc:
                attempt_feedback = f"\n\nTENTATIVA ANTERIOR REJEITADA (contrato): {str(exc)[:2000]}"
                continue
            problems = validate_storyboard(sb, set(tables), set(figures), {e["citekey"] for e in ledger_brief}, results)
            if rules.max_tabelas_figuras:
                principais = {x for s in sb.secoes if s.destino == "manuscrito" for p in s.paragrafos
                              for x in p.tabelas + p.figuras}
                if len(principais) > rules.max_tabelas_figuras:
                    problems.append(f"{len(principais)} tabelas+figuras no manuscrito; limite {rules.max_tabelas_figuras} "
                                    "— mova as excedentes para o Material Suplementar")
            if not problems:
                self.ctx.write_json(ws.storyboard, sb)
                return {"secoes": [s.id for s in sb.secoes], "suplementar": sb.itens_suplementares,
                        "limite_total": sb.limite_palavras_total}
            attempt_feedback = "\n\nTENTATIVA ANTERIOR REJEITADA:\n- " + "\n- ".join(problems[:30])
        raise GateBlocked(f"Storyboard inválido após 3 tentativas: {attempt_feedback[:2000]}")
