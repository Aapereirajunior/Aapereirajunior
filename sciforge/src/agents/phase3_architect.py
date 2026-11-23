"""Fase 3 — Arquiteto Epidemiológico + Revisor #2 (Red Team) → Protocolo.

1. O Arquiteto lê inventário + estado da arte + regras da revista e propõe 2-3 ângulos, escolhe um
   e redige o protocolo (PECO/PICO, objetivos, desenho, estratégia em R, sensibilidade, checklist).
2. O Revisor #2 ataca o protocolo (seleção, tempo imortal, confundimento residual, falácia
   ecológica, vazamento de dados, multiplicidade...).
3. O Arquiteto incorpora as defesas. Rodadas limitadas pelo BudgetGuard (reviewer_max_rounds).
Saída: 01_estado/03_protocolo_proposto.md + 03_critica_revisor2.json → PORTÃO HUMANO #1.
"""

from __future__ import annotations

from ..core.budget_guard import AttemptLimitReached
from ..core.schemas import ParecerRevisor, ProtocoloArquiteto
from ..tools.data_profiler import profile_to_prompt
from .base import PhaseAgent, compact_json


class Phase3Architect(PhaseAgent):
    phase = "phase3"
    title = "Fase 3 — Protocolo epidemiológico com Revisor #2"

    async def run(self) -> dict:
        inv = self.ctx.inventario()
        sota = self.ctx.ws.estado_da_arte.read_text(encoding="utf-8") if self.ctx.ws.estado_da_arte.exists() else ""
        rules = self.ctx.journal_rules_text()
        schema_txt = "\n\n".join(profile_to_prompt(p) for p in inv.schema_dados)
        inv_brief = compact_json(inv.model_dump(exclude={"schema_dados"}), 12000)
        context = (f"INVENTÁRIO DA SEMENTE:\n{inv_brief}\n\nPERFIL AGREGADO DOS DADOS:\n{schema_txt[:15000]}\n\n"
                   f"ESTADO DA ARTE (Fase 2):\n{sota[:30000]}\n\nREGRAS DO PERIÓDICO:\n{rules[:5000]}")

        draft = await self.cached_step("protocolo_v1", lambda: self.ctx.llm.complete_json(
            system=self.ctx.prompt("03_epidemiology_architect"),
            user=context + "\n\nProponha 2-3 ângulos, escolha o mais forte e redija o protocolo completo.",
            phase=self.phase, schema=ProtocoloArquiteto, role="default"))
        protocolo = ProtocoloArquiteto.model_validate(draft)

        pareceres: list[ParecerRevisor] = []
        rodada = 0
        while True:
            key = f"revisor_rodada_{rodada + 1}"
            if not self.ctx.state.step_done(self.phase, key):
                try:
                    self.ctx.budget.consume_attempt("reviewer", "phase3")
                except AttemptLimitReached:
                    break
            rodada += 1
            parecer = ParecerRevisor.model_validate(await self.cached_step(key, lambda: self.ctx.llm.complete_json(
                system=self.ctx.prompt("03b_reviewer2_redteam"),
                user=(f"{context[:20000]}\n\nPROTOCOLO SOB REVISÃO:\n{protocolo.protocolo_markdown}\n\n"
                      "Ataque o protocolo e emita o parecer no contrato JSON."),
                phase=self.phase, schema=ParecerRevisor, role="reviewer")))
            pareceres.append(parecer)
            graves = [c for c in parecer.criticas if c.gravidade in ("critica", "maior")]
            if parecer.aprovado and not graves:
                break
            revised = await self.cached_step(f"protocolo_v{rodada + 1}", lambda: self.ctx.llm.complete_json(
                system=self.ctx.prompt("03_epidemiology_architect"),
                user=(f"{context}\n\nPROTOCOLO ATUAL:\n{protocolo.protocolo_markdown}\n\n"
                      f"PARECER DO REVISOR #2:\n{compact_json(parecer)}\n\n"
                      "Incorpore defesas explícitas para CADA crítica (seção 'Ameaças à validade e defesas') "
                      "e devolva o protocolo revisado no mesmo contrato JSON."),
                phase=self.phase, schema=ProtocoloArquiteto, role="default"))
            protocolo = ProtocoloArquiteto.model_validate(revised)
            if self.ctx.budget.remaining("reviewer", "phase3") == 0:
                break

        escolhido = protocolo.angulos[min(protocolo.angulo_escolhido, len(protocolo.angulos) - 1)]
        header = [
            "<!-- Protocolo gerado pelo SciForge. Você pode editá-lo antes de aprovar o Portão #1. -->",
            f"<!-- checklist: {protocolo.checklist} | pacotes_r: {', '.join(protocolo.pacotes_r)} -->", "",
            "## Ângulos considerados", "",
        ]
        for i, a in enumerate(protocolo.angulos):
            marca = " **(escolhido)**" if a is escolhido else ""
            header.append(f"{i + 1}. **{a.titulo}**{marca} — {a.pergunta} _Desenho_: {a.desenho}. "
                          f"_Viabilidade_: {a.viabilidade_com_dados}")
        doc = protocolo.protocolo_markdown.strip() + "\n\n" + "\n".join(header) + "\n"
        self.ctx.write_text(self.ctx.ws.protocolo, doc)
        self.ctx.write_json(self.ctx.ws.critica_revisor, {
            "rodadas": [p.model_dump() for p in pareceres],
            "checklist": protocolo.checklist, "pacotes_r": protocolo.pacotes_r,
        })
        return {"angulo": escolhido.titulo, "checklist": protocolo.checklist, "rodadas_revisor": len(pareceres),
                "criticas_pendentes": [c.vies for p in pareceres[-1:] for c in p.criticas if c.gravidade == "critica"]}
