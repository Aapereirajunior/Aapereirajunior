"""Disjuntor de custo e de tentativas (Trava 8).

- Teto de tokens, custo (USD) e número de chamadas por fase, mais um teto global de custo.
- Limites rígidos de tentativas: auto-correção de script R (4), QA visual por figura (2),
  rodadas Redator ↔ Revisor #2 (2).
Os contadores vivem no SQLite do projeto, portanto sobrevivem a `sciforge resume`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .state_manager import StateManager


class BudgetExceeded(RuntimeError):
    """Disparado quando um teto de custo/tokens/chamadas é atingido."""


class AttemptLimitReached(RuntimeError):
    """Disparado quando um ciclo de auto-correção/QA/revisão esgota as tentativas permitidas."""


_LIMIT_KEYS = {
    "r_script": "r_script_max_retries",
    "figure_qa": "figure_qa_max_cycles",
    "reviewer": "reviewer_max_rounds",
    "firewall_regen": "firewall_regen_max",
}

_DEFAULT_LIMITS = {"r_script": 4, "figure_qa": 2, "reviewer": 2, "firewall_regen": 2}


@dataclass
class PhaseBudget:
    max_tokens: int
    max_cost_usd: float
    max_calls: int


class BudgetGuard:
    def __init__(self, settings: dict, state: StateManager):
        self.state = state
        cfg = settings.get("budget", {}) or {}
        self.global_max_cost = float(cfg.get("global_max_cost_usd", float("inf")))
        self.phase_budgets: dict[str, PhaseBudget] = {}
        for phase, b in (cfg.get("phases") or {}).items():
            self.phase_budgets[phase] = PhaseBudget(
                max_tokens=int(b.get("max_tokens", 10**12)),
                max_cost_usd=float(b.get("max_cost_usd", float("inf"))),
                max_calls=int(b.get("max_calls", 10**9)),
            )
        limits_cfg = cfg.get("limits") or {}
        self.limits = {kind: int(limits_cfg.get(key, _DEFAULT_LIMITS[kind])) for kind, key in _LIMIT_KEYS.items()}

    # ---------------------------------------------------------------- custo/tokens
    def check(self, phase: str, estimated_tokens: int = 0) -> None:
        """Verifica ANTES de uma chamada se ela cabe no orçamento da fase e no global."""
        usage = self.state.usage_by_phase(phase).get(phase, {"calls": 0, "tokens": 0, "cost": 0.0})
        budget = self.phase_budgets.get(phase)
        if budget:
            if usage["calls"] + 1 > budget.max_calls:
                raise BudgetExceeded(f"{phase}: limite de {budget.max_calls} chamadas de LLM atingido")
            if usage["tokens"] + estimated_tokens > budget.max_tokens:
                raise BudgetExceeded(
                    f"{phase}: limite de {budget.max_tokens} tokens seria excedido "
                    f"(usado {usage['tokens']}, estimado +{estimated_tokens})")
            if usage["cost"] >= budget.max_cost_usd:
                raise BudgetExceeded(f"{phase}: teto de US$ {budget.max_cost_usd:.2f} atingido")
        total = self.state.total_cost()
        if total >= self.global_max_cost:
            raise BudgetExceeded(f"Teto global de US$ {self.global_max_cost:.2f} atingido (gasto {total:.2f})")

    def record(self, phase: str, provider: str, model: str, input_tokens: int, output_tokens: int,
               cost_usd: float) -> None:
        self.state.record_usage(phase, provider, model, input_tokens, output_tokens, cost_usd)

    def summary(self) -> dict[str, dict[str, float]]:
        usage = self.state.usage_by_phase()
        out = {}
        for phase, b in self.phase_budgets.items():
            u = usage.get(phase, {"calls": 0, "tokens": 0, "cost": 0.0})
            out[phase] = {**u, "max_tokens": b.max_tokens, "max_cost_usd": b.max_cost_usd,
                          "max_calls": b.max_calls}
        return out

    # ---------------------------------------------------------------- tentativas
    def limit(self, kind: str) -> int:
        return self.limits[kind]

    def consume_attempt(self, kind: str, key: str) -> int:
        """Consome uma tentativa; dispara AttemptLimitReached se o limite já foi atingido."""
        used = self.state.attempts(kind, key)
        if used >= self.limits[kind]:
            raise AttemptLimitReached(
                f"Limite de {self.limits[kind]} tentativas para {kind}:{key} esgotado")
        return self.state.increment_attempt(kind, key)

    def remaining(self, kind: str, key: str) -> int:
        return max(0, self.limits[kind] - self.state.attempts(kind, key))
