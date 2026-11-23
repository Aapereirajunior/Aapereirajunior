"""Portabilidade Windows/macOS/Linux: localização de executáveis e subprocessos em UTF-8.

Nenhuma função aqui depende de shell: todos os comandos são listas de argumentos passadas a
`subprocess.run` sem `shell=True`, e toda saída é decodificada como UTF-8 (`errors="replace"`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _version_key(path: Path) -> tuple:
    """Ordena diretórios como R-4.3.2 < R-4.4.1 pela parte numérica."""
    parts = []
    for chunk in path.name.replace("R-", "").replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _first_existing(candidates: Iterable[Path]) -> Optional[str]:
    for c in candidates:
        if c and c.is_file():
            return str(c)
    return None


def _windows_program_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(var)
        if val:
            dirs.append(Path(val))
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Programs")
    return dirs


def _r_from_windows_registry() -> list[Path]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\R-core\R") as key:
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                found.append(Path(install_path) / "bin" / "Rscript.exe")
        except OSError:
            continue
    return found


def find_rscript() -> Optional[str]:
    """Localiza o Rscript: SCIFORGE_RSCRIPT → PATH → R_HOME → registro/Program Files (Windows) → locais padrão (macOS)."""
    env = os.environ.get("SCIFORGE_RSCRIPT")
    if env and Path(env).is_file():
        return str(Path(env))
    which = shutil.which("Rscript")
    if which:
        return which
    exe = "Rscript.exe" if IS_WINDOWS else "Rscript"
    candidates: list[Path] = []
    if os.environ.get("R_HOME"):
        candidates += [Path(os.environ["R_HOME"]) / "bin" / exe, Path(os.environ["R_HOME"]) / "bin" / "x64" / exe]
    if IS_WINDOWS:
        candidates += _r_from_windows_registry()
        for base in _windows_program_dirs():
            r_root = base / "R"
            if r_root.is_dir():
                for version_dir in sorted(r_root.glob("R-*"), key=_version_key, reverse=True):
                    candidates += [version_dir / "bin" / exe, version_dir / "bin" / "x64" / exe]
    elif IS_MAC:
        candidates += [Path("/Library/Frameworks/R.framework/Resources/bin/Rscript"),
                       Path("/opt/homebrew/bin/Rscript"), Path("/usr/local/bin/Rscript")]
    else:
        candidates += [Path("/usr/bin/Rscript"), Path("/usr/local/bin/Rscript"), Path("/opt/R/bin/Rscript")]
    return _first_existing(candidates)


def find_quarto() -> Optional[str]:
    """Localiza o Quarto: SCIFORGE_QUARTO → PATH → instalações padrão (Windows/macOS)."""
    env = os.environ.get("SCIFORGE_QUARTO")
    if env and Path(env).is_file():
        return str(Path(env))
    which = shutil.which("quarto")
    if which:
        return which
    candidates: list[Path] = []
    if IS_WINDOWS:
        for base in _windows_program_dirs():
            candidates.append(base / "Quarto" / "bin" / "quarto.exe")
    elif IS_MAC:
        candidates += [Path("/Applications/quarto/bin/quarto"), Path("/usr/local/bin/quarto")]
    else:
        candidates += [Path("/opt/quarto/bin/quarto"), Path("/usr/local/bin/quarto")]
    return _first_existing(candidates)


def find_pandoc() -> Optional[str]:
    """Pandoc do PATH ou o embutido no Quarto (bin/tools/[arquitetura/]pandoc)."""
    env = os.environ.get("SCIFORGE_PANDOC")
    if env and Path(env).is_file():
        return str(Path(env))
    which = shutil.which("pandoc")
    if which:
        return which
    quarto = find_quarto()
    if quarto:
        tools = Path(quarto).resolve().parent / "tools"
        exe = "pandoc.exe" if IS_WINDOWS else "pandoc"
        found = _first_existing([tools / exe, *sorted(tools.glob(f"*/{exe}"))])
        if found:
            return found
    return None


def find_pdftotext() -> Optional[str]:
    return shutil.which("pdftotext")


def run_command(args: Sequence[str], *, cwd: Path | None = None, env: dict | None = None,
                timeout: float | None = None) -> subprocess.CompletedProcess:
    """`subprocess.run` portátil: lista de argumentos, sem shell, saída decodificada em UTF-8."""
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd) if cwd else None, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
    )


def r_path(path: Path | str) -> str:
    """Caminho seguro para embutir em código R (barras normais também no Windows)."""
    return Path(path).resolve().as_posix()


def utf8_console() -> None:
    """No Windows, força stdout/stderr em UTF-8 (acentos e símbolos do Rich no console legado)."""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
