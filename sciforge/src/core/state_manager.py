"""Persistência granular do estado em SQLite (Trava 8).

Cada fase, cada etapa interna (ex.: cada seção redigida na Fase 6), cada chamada de LLM e cada
tentativa de auto-correção é gravada. `sciforge resume` usa estas tabelas para continuar exatamente
do ponto de interrupção sem refazer trabalho concluído.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .schemas import now_iso

PHASES = ["phase1", "phase2", "phase3", "phase4", "phase5", "phase6"]
GATES = {"gate1": "phase3", "gate2": "phase4", "gate3": "phase6"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS phases (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- pending | running | done | failed | blocked
    started_at TEXT,
    finished_at TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    phase TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,              -- done | failed
    payload TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (phase, key)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS gates (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- pending | approved | rejected
    note TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class StateManager:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ fases
    def set_phase(self, phase: str, status: str, message: str | None = None) -> None:
        if phase not in PHASES:
            raise ValueError(f"Fase desconhecida: {phase}")
        ts = now_iso()
        with self._conn() as conn:
            row = conn.execute("SELECT started_at FROM phases WHERE name=?", (phase,)).fetchone()
            started = row["started_at"] if row and row["started_at"] else (ts if status == "running" else None)
            finished = ts if status in {"done", "failed", "blocked"} else None
            conn.execute(
                "INSERT INTO phases(name,status,started_at,finished_at,message) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, started_at=excluded.started_at, "
                "finished_at=excluded.finished_at, message=excluded.message",
                (phase, status, started, finished, message),
            )

    def phase_status(self, phase: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM phases WHERE name=?", (phase,)).fetchone()
        return row["status"] if row else "pending"

    def phases(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM phases")}
        return [rows.get(p, {"name": p, "status": "pending", "started_at": None,
                             "finished_at": None, "message": None}) for p in PHASES]

    def next_phase(self) -> Optional[str]:
        for p in PHASES:
            if self.phase_status(p) != "done":
                return p
        return None

    def reset_from(self, phase: str) -> None:
        """Invalida a fase indicada e todas as posteriores (útil após editar um artefato manualmente)."""
        idx = PHASES.index(phase)
        targets = PHASES[idx:]
        with self._conn() as conn:
            for p in targets:
                conn.execute("DELETE FROM phases WHERE name=?", (p,))
                conn.execute("DELETE FROM steps WHERE phase=?", (p,))
            for gate, ph in GATES.items():
                if ph in targets:
                    conn.execute("DELETE FROM gates WHERE name=?", (gate,))

    # ------------------------------------------------------------------ etapas
    def save_step(self, phase: str, key: str, payload: Any = None, status: str = "done") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO steps(phase,key,status,payload,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(phase,key) DO UPDATE SET status=excluded.status, payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (phase, key, status, json.dumps(payload, ensure_ascii=False, default=str), now_iso()),
            )

    def get_step(self, phase: str, key: str) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM steps WHERE phase=? AND key=? AND status='done'", (phase, key)
            ).fetchone()
        return json.loads(row["payload"]) if row and row["payload"] is not None else None

    def step_done(self, phase: str, key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM steps WHERE phase=? AND key=? AND status='done'", (phase, key)
            ).fetchone()
        return row is not None

    def delete_step(self, phase: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM steps WHERE phase=? AND key=?", (phase, key))

    def steps(self, phase: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT phase,key,status,updated_at FROM steps WHERE phase=? ORDER BY updated_at", (phase,))]

    # ------------------------------------------------------------------ uso de LLM
    def record_usage(self, phase: str, provider: str, model: str, input_tokens: int,
                     output_tokens: int, cost_usd: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO usage(phase,provider,model,input_tokens,output_tokens,cost_usd,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (phase, provider, model, int(input_tokens), int(output_tokens), float(cost_usd), now_iso()),
            )

    def usage_by_phase(self, phase: str | None = None) -> dict[str, dict[str, float]]:
        sql = ("SELECT phase, COUNT(*) calls, SUM(input_tokens+output_tokens) tokens, SUM(cost_usd) cost "
               "FROM usage {} GROUP BY phase")
        with self._conn() as conn:
            if phase:
                rows = conn.execute(sql.format("WHERE phase=?"), (phase,)).fetchall()
            else:
                rows = conn.execute(sql.format("")).fetchall()
        return {r["phase"]: {"calls": r["calls"], "tokens": r["tokens"] or 0, "cost": r["cost"] or 0.0}
                for r in rows}

    def total_cost(self) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM usage").fetchone()
        return float(row["c"])

    # ------------------------------------------------------------------ tentativas
    def increment_attempt(self, kind: str, key: str) -> int:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO attempts(kind,key,count) VALUES(?,?,1) "
                "ON CONFLICT(kind,key) DO UPDATE SET count=count+1", (kind, key))
            row = conn.execute("SELECT count FROM attempts WHERE kind=? AND key=?", (kind, key)).fetchone()
        return int(row["count"])

    def attempts(self, kind: str, key: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT count FROM attempts WHERE kind=? AND key=?", (kind, key)).fetchone()
        return int(row["count"]) if row else 0

    def reset_attempts(self, kind: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM attempts WHERE kind=? AND key=?", (kind, key))

    # ------------------------------------------------------------------ portões humanos
    def set_gate(self, gate: str, status: str, note: str | None = None) -> None:
        if gate not in GATES:
            raise ValueError(f"Portão desconhecido: {gate}")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO gates(name,status,note,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, note=excluded.note, "
                "updated_at=excluded.updated_at", (gate, status, note, now_iso()))

    def gate_status(self, gate: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM gates WHERE name=?", (gate,)).fetchone()
        return row["status"] if row else "pending"

    # ------------------------------------------------------------------ log
    def log(self, message: str, level: str = "info", phase: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO events(phase,level,message,created_at) VALUES(?,?,?,?)",
                         (phase, level, message, now_iso()))

    def events(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]
