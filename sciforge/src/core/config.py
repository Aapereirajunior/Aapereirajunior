"""Carregamento de configuração, caminhos do sistema e do workspace, e prompts externos."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv é opcional em tempo de execução
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def sciforge_home() -> Path:
    """Raiz do SciForge (contém config/, prompts/, scripts_r/, workspace_template/)."""
    env = os.environ.get("SCIFORGE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


HOME = sciforge_home()
CONFIG_DIR = HOME / "config"
PROMPTS_DIR = HOME / "prompts"
SCRIPTS_R_DIR = HOME / "scripts_r"
TEMPLATE_DIR = HOME / "workspace_template"

if load_dotenv is not None:
    load_dotenv(HOME / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_settings(project_dir: Path | None = None, extra: dict | None = None) -> dict[str, Any]:
    """Lê config/settings.yaml e aplica override do projeto (01_estado/settings.override.yaml)."""
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as fh:
        settings = yaml.safe_load(fh) or {}
    if project_dir is not None:
        override = Path(project_dir) / "01_estado" / "settings.override.yaml"
        if override.exists():
            with open(override, encoding="utf-8") as fh:
                settings = _deep_merge(settings, yaml.safe_load(fh) or {})
    if extra:
        settings = _deep_merge(settings, extra)
    mode = settings.get("privacy_mode", "hybrid")
    if mode not in {"strict", "hybrid", "cloud"}:
        raise ValueError(f"privacy_mode inválido: {mode!r} (use strict | hybrid | cloud)")
    return settings


def load_prompt(name: str) -> str:
    """Carrega um system prompt de prompts/<name>.md (o pesquisador pode editá-los livremente)."""
    path = PROMPTS_DIR / (name if name.endswith(".md") else f"{name}.md")
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Workspace:
    """Caminhos canônicos de um projeto criado por `sciforge init`."""

    root: Path

    @property
    def semente(self) -> Path:
        return self.root / "00_semente"

    @property
    def dados(self) -> Path:
        return self.semente / "dados"

    @property
    def graficos_preliminares(self) -> Path:
        return self.semente / "graficos_preliminares"

    @property
    def regras_revista(self) -> Path:
        return self.semente / "regras_revista.txt"

    @property
    def estado(self) -> Path:
        return self.root / "01_estado"

    @property
    def analise(self) -> Path:
        return self.root / "02_analise"

    @property
    def entrega(self) -> Path:
        return self.root / "03_entrega"

    @property
    def tabelas(self) -> Path:
        return self.entrega / "tabelas"

    @property
    def figuras(self) -> Path:
        return self.entrega / "figuras"

    @property
    def db_path(self) -> Path:
        return self.estado / "sciforge_state.sqlite"

    # Artefatos estruturados
    @property
    def inventario(self) -> Path:
        return self.estado / "01_inventario_semente.json"

    @property
    def estado_da_arte(self) -> Path:
        return self.estado / "02_estado_da_arte.md"

    @property
    def evidence_ledger(self) -> Path:
        return self.estado / "02_evidence_ledger.json"

    @property
    def corpus(self) -> Path:
        return self.estado / "02_corpus.jsonl"

    @property
    def bib_pool(self) -> Path:
        return self.estado / "referencias_pool.bib"

    @property
    def protocolo(self) -> Path:
        return self.estado / "03_protocolo_proposto.md"

    @property
    def critica_revisor(self) -> Path:
        return self.estado / "03_critica_revisor2.json"

    @property
    def resultados(self) -> Path:
        return self.estado / "04_resultados_verificados.json"

    @property
    def storyboard(self) -> Path:
        return self.estado / "05_storyboard.json"

    @property
    def firewall_fingerprint(self) -> Path:
        return self.estado / ".firewall_fingerprint.json"

    def draft_files(self) -> list[Path]:
        """Arquivos de rascunho aceitos (somente a Fase 1 pode abri-los — ver integrity_checks.guarded_read)."""
        exts = (".md", ".txt", ".pdf", ".docx")
        return sorted(p for p in self.semente.glob("rascunho.*") if p.suffix.lower() in exts)

    def data_files(self) -> list[Path]:
        exts = {".parquet", ".csv", ".rds", ".fst", ".tsv"}
        return sorted(p for p in self.dados.glob("*") if p.suffix.lower() in exts)

    def ensure(self) -> None:
        for p in (self.semente, self.dados, self.graficos_preliminares, self.estado, self.analise,
                  self.entrega, self.tabelas, self.figuras):
            p.mkdir(parents=True, exist_ok=True)
