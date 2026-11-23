"""Infraestrutura comum dos agentes de fase."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.budget_guard import BudgetGuard
from ..core.config import Workspace, load_prompt, load_settings
from ..core.llm_client import LLMClient
from ..core.schemas import EvidenceLedger, InventarioSemente
from ..core.state_manager import StateManager
from ..tools.integrity_checks import DraftFirewall, FirewallViolation, guarded_read


class GateBlocked(RuntimeError):
    """A fase concluiu mas detectou condição que impede avançar ao portão humano."""


@dataclass
class PhaseContext:
    ws: Workspace
    settings: dict
    state: StateManager
    budget: BudgetGuard
    llm: LLMClient
    log: Callable[[str], None] = field(default=print)

    @classmethod
    def create(cls, project_dir: Path, settings_extra: dict | None = None, llm: LLMClient | None = None,
               log: Callable[[str], None] = print) -> "PhaseContext":
        ws = Workspace(Path(project_dir).resolve())
        ws.ensure()
        settings = load_settings(ws.root, settings_extra)
        state = StateManager(ws.db_path)
        budget = BudgetGuard(settings, state)
        firewall = DraftFirewall.load(ws.firewall_fingerprint)
        guard = firewall.guard if firewall else None
        if llm is None:
            llm = LLMClient(settings, budget, prompt_guard=guard)
        else:
            llm.budget = llm.budget or budget
            llm.prompt_guard = llm.prompt_guard or guard
        return cls(ws, settings, state, budget, llm, log)

    def activate_firewall(self, draft_text: str) -> None:
        integ = self.settings.get("integrity", {})
        fw = DraftFirewall.from_draft(draft_text, int(integ.get("firewall_ngram_n", 10)),
                                      int(integ.get("firewall_max_shared_ngrams", 2)),
                                      int(integ.get("draft_ngram_n", 6)))
        fw.save(self.ws.firewall_fingerprint)
        self.llm.prompt_guard = fw.guard

    # -------------------------------------------------------------- leitura protegida de artefatos
    def read(self, path: Path, phase: str) -> str:
        return guarded_read(path, phase, self.ws.semente)

    def read_json(self, path: Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def write_json(self, path: Path, data: Any) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json")
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_text(self, path: Path, text: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    def inventario(self) -> InventarioSemente:
        return InventarioSemente.model_validate(self.read_json(self.ws.inventario))

    def ledger(self) -> EvidenceLedger:
        if not self.ws.evidence_ledger.exists():
            return EvidenceLedger()
        return EvidenceLedger.model_validate(self.read_json(self.ws.evidence_ledger))

    def journal_rules_text(self) -> str:
        p = self.ws.regras_revista
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def language(self) -> str:
        from ..tools.quarto_renderer import parse_journal_rules
        rules = parse_journal_rules(self.journal_rules_text())
        inv_lang = None
        if self.ws.inventario.exists():
            inv_lang = (self.inventario().restricoes_e_preferencias_autor or {}).get("idioma")
        return rules.idioma or inv_lang or self.settings.get("idioma", "pt-BR")

    def prompt(self, name: str) -> str:
        return load_prompt(name)


class PhaseAgent(ABC):
    phase: str = ""
    title: str = ""

    def __init__(self, ctx: PhaseContext):
        self.ctx = ctx

    @abstractmethod
    async def run(self) -> dict:
        """Executa a fase e devolve um resumo serializável."""

    async def execute(self) -> dict:
        st = self.ctx.state
        st.set_phase(self.phase, "running")
        st.log(f"Início: {self.title}", phase=self.phase)
        try:
            result = await self.run()
        except GateBlocked as exc:
            st.set_phase(self.phase, "blocked", str(exc))
            st.log(f"Bloqueado: {exc}", level="error", phase=self.phase)
            raise
        except Exception as exc:
            st.set_phase(self.phase, "failed", f"{type(exc).__name__}: {exc}")
            st.log(f"Falha: {exc}", level="error", phase=self.phase)
            raise
        st.set_phase(self.phase, "done", json.dumps(result, ensure_ascii=False, default=str)[:2000])
        st.log(f"Concluído: {self.title}", phase=self.phase)
        return result

    async def cached_step(self, key: str, fn: Callable[[], Any]) -> Any:
        """Executa uma etapa apenas se ainda não concluída (retomada granular)."""
        cached = self.ctx.state.get_step(self.phase, key)
        if cached is not None:
            self.ctx.log(f"  ↺ {self.phase}/{key} já concluído — reutilizando")
            return cached
        result = fn()
        if hasattr(result, "__await__"):
            result = await result
        self.ctx.state.save_step(self.phase, key, result)
        return result


def compact_json(data: Any, max_chars: int = 30000) -> str:
    text = json.dumps(data.model_dump(mode="json") if hasattr(data, "model_dump") else data,
                      ensure_ascii=False, indent=1, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... [truncado]"


__all__ = ["PhaseContext", "PhaseAgent", "GateBlocked", "FirewallViolation", "compact_json"]
