"""Wrapper Python para `scripts_r/profile_data.R` (somente leitura, apenas agregados)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ..core.config import SCRIPTS_R_DIR
from ..core.platform_utils import find_rscript, run_command
from ..core.schemas import DatasetProfile
from .r_sandbox import sanitize_output

SUPPORTED = {".csv", ".tsv", ".parquet", ".rds", ".fst"}


class ProfilerError(RuntimeError):
    """Falha ao executar ou validar o profiling em R."""


def profile_dataset(path: Path | str, min_cell: int = 5, timeout_s: int = 600) -> DatasetProfile:
    """Executa o Raio-X em R e devolve o perfil validado. O arquivo de dados nunca é alterado."""
    path = Path(path).resolve()
    if path.suffix.lower() not in SUPPORTED:
        raise ProfilerError(f"Formato não suportado: {path.suffix}")
    if not path.exists():
        raise ProfilerError(f"Arquivo não encontrado: {path}")
    rscript = find_rscript()
    if rscript is None:
        raise ProfilerError("Rscript não encontrado no PATH. Instale o R para usar o profiler.")
    mtime_before = path.stat().st_mtime_ns
    with tempfile.TemporaryDirectory(prefix="sciforge_profile_") as tmp:
        out_json = Path(tmp) / "perfil.json"
        try:
            proc = run_command([rscript, "--vanilla", SCRIPTS_R_DIR / "profile_data.R", path, out_json, str(min_cell)],
                               timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ProfilerError(f"profile_data.R excedeu {timeout_s}s") from exc
        if proc.returncode != 0:
            raise ProfilerError(f"profile_data.R falhou:\n{sanitize_output(proc.stderr)[-3000:]}")
        data = json.loads(out_json.read_text(encoding="utf-8"))
    if path.stat().st_mtime_ns != mtime_before:
        raise ProfilerError("O arquivo de dados foi modificado durante o profiling — violação de somente leitura.")
    # jsonlite grava listas vazias como [] e strings únicas como escalares; normalizamos.
    for col in data.get("colunas", []):
        col.setdefault("niveis", [])
        if isinstance(col.get("niveis"), dict):
            col["niveis"] = [col["niveis"]]
    if isinstance(data.get("avisos"), str):
        data["avisos"] = [data["avisos"]]
    return DatasetProfile.model_validate(data)


def profile_directory(directory: Path, min_cell: int = 5) -> list[DatasetProfile]:
    files = sorted(p for p in Path(directory).glob("*") if p.suffix.lower() in SUPPORTED)
    return [profile_dataset(p, min_cell=min_cell) for p in files]


def profile_to_prompt(profile: DatasetProfile, max_levels: int = 8) -> str:
    """Representação compacta e agregada de um perfil, segura para prompts."""
    lines = [f"Arquivo {profile.arquivo} ({profile.formato}, engine {profile.engine}): "
             f"{profile.n_linhas} linhas × {profile.n_colunas} colunas"]
    for c in profile.colunas:
        desc = f"- {c.nome} [{c.tipo}] missing={c.pct_missing}%"
        if c.possivel_identificador:
            desc += " (IDENTIFICADOR — não usar como variável analítica)"
        elif c.media is not None:
            desc += f" média={c.media} dp={c.dp} mediana={c.mediana} IIQ=[{c.p25}; {c.p75}] faixa=[{c.minimo}; {c.maximo}]"
        elif c.data_min:
            desc += f" período={c.data_min} a {c.data_max}"
        elif c.niveis:
            lv = ", ".join(f"{n.nivel}={'<5' if n.suprimido else n.n}" for n in c.niveis[:max_levels])
            desc += f" níveis: {lv}"
        lines.append(desc)
    if profile.avisos:
        lines.append("Avisos: " + "; ".join(profile.avisos))
    return "\n".join(lines)
