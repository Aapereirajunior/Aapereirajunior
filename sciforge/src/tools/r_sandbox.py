"""Executor seguro de Rscript (Trava 5).

1. Análise estática ANTES da execução: bloqueia código que exiba registros individuais
   (head/tail/View/glimpse/print de data frames, auto-print de objetos de dados) e chamadas
   perigosas (system, download, escrita fora do workspace).
2. Execução com timeout em diretório controlado, com variáveis de ambiente que apontam o script
   para o workspace e para os helpers padronizados.
3. Sanitização LGPD de stdout/stderr: CPF, CNS, datas, e-mails, telefones e sequências longas de
   dígitos são mascarados antes que qualquer log volte a um modelo de linguagem.
4. Laço de auto-correção limitado pelo BudgetGuard (máx. 4 tentativas por script).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..core.budget_guard import AttemptLimitReached, BudgetGuard
from ..core.config import SCRIPTS_R_DIR
from ..core.platform_utils import find_rscript, run_command

# --------------------------------------------------------------------------------------------
# Sanitizador de PII
# --------------------------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("CPF", re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")),
    ("CNS", re.compile(r"(?<!\d)[12789]\d{2}\s?\d{4}\s?\d{4}\s?\d{4}(?!\d)")),
    ("CPF", re.compile(r"(?<![\d.])\d{11}(?![\d.])")),
    ("TELEFONE", re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-\d{4}(?!\d)")),
    ("DATA", re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])/(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}(?!\d)")),
    ("DATA", re.compile(r"(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\d])")),
    ("NUMERO_LONGO", re.compile(r"(?<![\d.])\d{12,}(?![\d.])")),
]


def sanitize_output(text: str) -> str:
    """Mascara padrões potenciais de PII em logs (stdout/stderr) antes de enviá-los a qualquer LLM."""
    if not text:
        return text
    out = text
    for label, pattern in _PII_PATTERNS:
        out = pattern.sub(f"[{label}_MASCARADO]", out)
    return out


# --------------------------------------------------------------------------------------------
# Análise estática
# --------------------------------------------------------------------------------------------

_ALWAYS_BLOCKED = [
    (re.compile(r"\bView\s*\("), "View() abre registros individuais"),
    (re.compile(r"\bglimpse\s*\("), "glimpse() imprime valores de linhas individuais"),
    (re.compile(r"\bsystem2?\s*\("), "chamadas ao sistema operacional são proibidas no sandbox"),
    (re.compile(r"\bshell(\.exec)?\s*\("), "chamadas ao shell são proibidas no sandbox"),
    (re.compile(r"\bdownload\.file\s*\("), "downloads são proibidos durante a análise"),
    (re.compile(r"\burl\s*\(\s*['\"]https?://"), "conexões de rede são proibidas durante a análise"),
    (re.compile(r"\bhttr2?::"), "pacotes de rede são proibidos durante a análise"),
    (re.compile(r"\bunlink\s*\(|\bfile\.remove\s*\("), "remoção de arquivos é proibida"),
    (re.compile(r"\bsetwd\s*\(\s*['\"]/"), "setwd() para caminho absoluto é proibido"),
    (re.compile(r"\bwrite(_csv|\.csv|\.table|_tsv|_parquet)?\s*\(.*00_semente"),
     "escrita dentro de 00_semente é proibida (dados originais são somente leitura)"),
]

_HEAD_TAIL = re.compile(r"\b(?:utils::)?(head|tail)\s*\(\s*([^,)]+)")
_PRINT = re.compile(r"\bprint\s*\(\s*([A-Za-z.][\w.]*)\s*[,)]")
_SAFE_HEAD_ARGS = re.compile(r"^\s*(names|colnames|levels|sort|order|unique|table|rev|seq|letters|LETTERS|c)\b")
_DF_ASSIGN = re.compile(
    r"^\s*([A-Za-z.][\w.]*)\s*(?:<-|=)\s*(?!function)(.*)$")
_DF_HINTS = re.compile(
    r"(read_csv|read\.csv|fread|read_parquet|open_dataset|readRDS|read_rds|read_fst|read_dta|read_sav|"
    r"read_excel|data\.frame|tibble|as_tibble|as\.data\.frame|dbGetQuery|collect\s*\(|%>%|\|>|"
    r"\bmutate\s*\(|\bfilter\s*\(|\bselect\s*\(|left_join|inner_join|bind_rows|merge\s*\(|"
    r"sf_flow_start|sf_flow_exclude|\[\s*[^]]*,\s*\])")
_SAFE_NAME_HINT = re.compile(r"(_tbl|_table|_fit|_mod|_model|_gt|_plot|_fig|_res|_summary|_diag)$|^(tbl|fig|fit|mod|p)_")


@dataclass
class StaticViolation:
    line: int
    code: str
    reason: str

    def __str__(self) -> str:
        return f"linha {self.line}: {self.reason} → `{self.code.strip()[:120]}`"


def _strip_comment(line: str) -> str:
    in_str: Optional[str] = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == in_str and line[i - 1] != "\\":
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "#":
            return line[:i]
    return line


def static_check(code: str) -> list[StaticViolation]:
    """Retorna as violações de privacidade/segurança encontradas no código R gerado."""
    violations: list[StaticViolation] = []
    data_objects: set[str] = set()
    lines = code.splitlines()
    depth = 0
    for idx, raw in enumerate(lines, start=1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        for pattern, reason in _ALWAYS_BLOCKED:
            if pattern.search(line):
                violations.append(StaticViolation(idx, raw, reason))
        m = _DF_ASSIGN.match(line)
        if m and _DF_HINTS.search(m.group(2)) and not _SAFE_NAME_HINT.search(m.group(1)):
            data_objects.add(m.group(1))
        for hm in _HEAD_TAIL.finditer(line):
            arg = hm.group(2)
            if not _SAFE_HEAD_ARGS.match(arg):
                violations.append(StaticViolation(
                    idx, raw, f"{hm.group(1)}() exibe registros individuais; use sf_safe_summary() ou agregados"))
        for pm in _PRINT.finditer(line):
            if pm.group(1) in data_objects:
                violations.append(StaticViolation(
                    idx, raw, f"print() direto do data frame '{pm.group(1)}' expõe registros"))
        stripped = line.strip()
        # Auto-print no nível superior: uma linha contendo só o nome de um objeto de dados.
        if depth == 0 and stripped in data_objects:
            violations.append(StaticViolation(idx, raw, f"auto-print do data frame '{stripped}' no nível superior"))
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return violations


# --------------------------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------------------------

@dataclass
class RunResult:
    script: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    violations: list[StaticViolation] = field(default_factory=list)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.violations and not self.timed_out

    def feedback(self, max_chars: int = 6000) -> str:
        """Resumo sanitizado para devolver ao modelo (já sem PII)."""
        if self.violations:
            return "VIOLAÇÕES DE PRIVACIDADE/SEGURANÇA (script não executado):\n" + "\n".join(
                f"- {v}" for v in self.violations)
        parts = [f"exit_code={self.exit_code}{' (timeout)' if self.timed_out else ''}"]
        if self.stderr:
            parts.append("STDERR (últimas linhas):\n" + self.stderr[-max_chars:])
        if self.stdout:
            parts.append("STDOUT (últimas linhas):\n" + self.stdout[-max_chars // 2:])
        return "\n\n".join(parts)


class RSandbox:
    def __init__(self, workspace_root: Path, analysis_dir: Path | None = None, timeout_s: int = 1800,
                 rscript: str | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.analysis_dir = Path(analysis_dir or self.workspace_root / "02_analise").resolve()
        self.timeout_s = timeout_s
        self.rscript = rscript or find_rscript()

    @property
    def available(self) -> bool:
        return self.rscript is not None

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            # Barras normais: o R aceita em qualquer SO e evita escapes de "\\" no Windows.
            "SCIFORGE_WORKSPACE": self.workspace_root.as_posix(),
            "SCIFORGE_R_HELPERS": (SCRIPTS_R_DIR / "export_table_helper.R").as_posix(),
            "R_LIBS_USER": env.get("R_LIBS_USER", ""),
            "LANG": env.get("LANG", "C.UTF-8"),
        })
        for secret in ("GEMINI_API_KEY", "OPENAI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"):
            env.pop(secret, None)
        return env

    def run_file(self, script: Path, args: list[str] | None = None, check_static: bool = True,
                 cwd: Path | None = None) -> RunResult:
        script = Path(script).resolve()
        code = script.read_text(encoding="utf-8")
        violations = static_check(code) if check_static else []
        if violations:
            return RunResult(script.name, -1, "", "", 0.0, violations)
        if not self.available:
            raise RuntimeError("Rscript não encontrado no PATH. Instale o R (https://cran.r-project.org).")
        start = time.monotonic()
        try:
            proc = run_command([self.rscript, script, *(args or [])], cwd=cwd or self.analysis_dir,
                               env=self.env(), timeout=self.timeout_s)
            return RunResult(script.name, proc.returncode, sanitize_output(proc.stdout),
                             sanitize_output(proc.stderr), time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return RunResult(script.name, -9, sanitize_output(out), sanitize_output(err),
                             time.monotonic() - start, timed_out=True)

    def run_code(self, code: str, name: str = "snippet.R", **kw) -> RunResult:
        path = self.analysis_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return self.run_file(path, **kw)

    async def run_with_autofix(
        self, script: Path, fixer: Callable[[str, str, int], Awaitable[str]], budget: BudgetGuard | None = None,
        max_attempts: int = 4, extra_validator: Callable[[], Optional[str]] | None = None,
    ) -> RunResult:
        """Executa `script`; em caso de falha chama `fixer(codigo, feedback, tentativa)` e tenta de novo.

        `extra_validator` roda após exit 0 e retorna uma mensagem de erro (ex.: contrato de diagnósticos
        violado) ou None — permitindo que "exit 0 mas estatisticamente inválido" também acione correção.
        """
        script = Path(script)
        key = script.name
        attempt = 0
        result: RunResult | None = None
        while True:
            result = self.run_file(script)
            problem: Optional[str] = None
            if result.ok and extra_validator is not None:
                problem = extra_validator()
            if result.ok and problem is None:
                return result
            feedback = result.feedback() if not result.ok else f"VALIDAÇÃO PÓS-EXECUÇÃO FALHOU:\n{problem}"
            try:
                if budget is not None:
                    attempt = budget.consume_attempt("r_script", key)
                else:
                    attempt += 1
                    if attempt > max_attempts:
                        raise AttemptLimitReached(f"Limite de {max_attempts} tentativas para {key}")
            except AttemptLimitReached:
                if not result.ok:
                    return result
                return RunResult(result.script, 99, result.stdout, (result.stderr + "\n" + feedback).strip(),
                                 result.duration_s)
            new_code = await fixer(script.read_text(encoding="utf-8"), feedback, attempt)
            script.write_text(new_code, encoding="utf-8")
