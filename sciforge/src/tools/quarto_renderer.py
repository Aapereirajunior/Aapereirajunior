"""Montagem de `.qmd`, gestão de estilos CSL, `quarto render` e Kit de Submissão (Trava 4 e 7).

- Tabelas entram SEMPRE como chunks R que apenas fazem `readRDS()` do objeto gerado pela Fase 4
  (nenhuma célula é transcrita pelo LLM).
- Figuras entram como imagens com label `{#fig-...}` para cross-reference nativa do Quarto.
- Estilos CSL: Vancouver/ABNT locais em config/csl; qualquer outro é baixado do repositório
  oficial `citation-style-language/styles` pelo nome.
- Se o Quarto não estiver instalado, `render_with_pandoc_fallback` gera .docx via Pandoc + citeproc
  substituindo os chunks pelas prévias .md das tabelas (marcando o documento como prévia).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import httpx
import yaml

from ..core.config import CONFIG_DIR
from ..core.platform_utils import find_pandoc, find_quarto, run_command

CSL_DIR = CONFIG_DIR / "csl"
CSL_REPO_RAW = "https://raw.githubusercontent.com/citation-style-language/styles/master"
CSL_ALIASES = {
    "vancouver": "vancouver",          # config/csl/vancouver.csl (cópia de elsevier-vancouver, numérico ICMJE)
    "icmje": "vancouver",
    "abnt": "abnt",
    "associacao-brasileira-de-normas-tecnicas": "abnt",
    "apa": "apa",
    "ama": "american-medical-association",
    "lancet": "the-lancet",
    "bmj": "bmj",
    "plos": "plos",
    "elsevier-vancouver": "elsevier-vancouver",
    "cadernos-de-saude-publica": "cadernos-de-saude-publica",
    "revista-de-saude-publica": "revista-de-saude-publica",
}


class QuartoNotFound(RuntimeError):
    """Quarto (ou Pandoc, no fallback) não está instalado."""


# ============================================================================================
# CSL
# ============================================================================================

def ensure_csl(style: str, dest_dir: Path, transport: httpx.BaseTransport | None = None) -> Path:
    """Copia o .csl local ou baixa do repositório oficial (tenta raiz e `dependent/`)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = CSL_ALIASES.get(style.strip().lower().removesuffix(".csl"), style.strip().lower().removesuffix(".csl"))
    local = CSL_DIR / f"{name}.csl"
    target = dest_dir / "estilo_alvo.csl"
    if local.exists():
        shutil.copyfile(local, target)
        return target
    with httpx.Client(timeout=30, transport=transport, follow_redirects=True) as client:
        for sub in ("", "dependent/"):
            resp = client.get(f"{CSL_REPO_RAW}/{sub}{name}.csl")
            if resp.status_code == 200 and "<style" in resp.text:
                text = resp.text
                # Estilos dependentes apontam para um estilo-pai: baixa o pai (independente).
                parent = re.search(r'<link[^>]+rel="independent-parent"[^>]+href="[^"]*/styles/([^"/]+)"', text)
                if sub == "dependent/" and parent:
                    presp = client.get(f"{CSL_REPO_RAW}/{parent.group(1)}.csl")
                    if presp.status_code == 200:
                        text = presp.text
                CSL_DIR.mkdir(parents=True, exist_ok=True)
                (CSL_DIR / f"{name}.csl").write_text(text, encoding="utf-8")
                target.write_text(text, encoding="utf-8")
                return target
    raise FileNotFoundError(f"Estilo CSL '{style}' não encontrado localmente nem em citation-style-language/styles")


# ============================================================================================
# Regras da revista
# ============================================================================================

@dataclass
class JournalRules:
    nome: Optional[str] = None
    limite_palavras: Optional[int] = None
    limite_resumo: Optional[int] = None
    estilo_csl: Optional[str] = None
    idioma: Optional[str] = None
    estrutura_resumo: list[str] = field(default_factory=list)
    max_tabelas_figuras: Optional[int] = None
    max_referencias: Optional[int] = None
    texto: str = ""


def parse_journal_rules(text: str) -> JournalRules:
    """Extrai regras estruturadas de `regras_revista.txt` (formato livre com rótulos comuns)."""
    r = JournalRules(texto=text or "")
    low = (text or "").lower()

    def num(patterns: Iterable[str]) -> Optional[int]:
        for p in patterns:
            m = re.search(p, low)
            if m:
                return int(re.sub(r"\D", "", m.group(1)))
        return None
    r.limite_palavras = num([r"limite (?:de )?palavras(?: do (?:texto|manuscrito|artigo))?\s*[:=]\s*([\d.]+)",
                             r"word (?:limit|count)\s*[:=]\s*([\d,]+)", r"máximo de ([\d.]+) palavras"])
    r.limite_resumo = num([r"(?:limite do )?resumo\s*[:=]\s*(?:até |máximo )?([\d.]+) palavras",
                           r"abstract\s*[:=]\s*(?:up to )?([\d,]+) words"])
    r.max_tabelas_figuras = num([r"(?:tabelas e figuras|tabelas/figuras|tables and figures)\s*[:=]\s*(?:até |máx\.? )?(\d+)"])
    r.max_referencias = num([r"(?:referências|references)\s*[:=]\s*(?:até |máx\.? )?(\d+)"])
    m = re.search(r"(?:estilo|style|csl)\s*(?:de citação|citation)?\s*[:=]\s*([\w\- ]+)", low)
    if m:
        r.estilo_csl = m.group(1).strip().replace(" ", "-")
    m = re.search(r"(?:idioma|language)\s*[:=]\s*([\w\- ]+)", low)
    if m:
        lang = m.group(1).strip()
        r.idioma = "en" if lang.startswith(("en", "ingl")) else "pt-BR" if lang.startswith(("pt", "portug")) else lang
    m = re.search(r"(?:revista|periódico|journal)\s*[:=]\s*(.+)", text or "", flags=re.I)
    if m:
        r.nome = m.group(1).strip()
    m = re.search(r"(?:estrutura do resumo|abstract structure)\s*[:=]\s*(.+)", text or "", flags=re.I)
    if m:
        r.estrutura_resumo = [s.strip() for s in re.split(r"[;,/]", m.group(1)) if s.strip()]
    return r


# ============================================================================================
# Montagem do .qmd
# ============================================================================================

def table_chunk(label: str, rds_relpath: str, caption: str) -> str:
    """Chunk R determinístico: só carrega o objeto compilado pela Fase 4."""
    if not label.startswith("tbl-"):
        raise ValueError("Labels de tabela devem começar com 'tbl-'")
    cap = caption.replace('"', "'")
    return (
        "```{r}\n"
        f"#| label: {label}\n"
        f"#| tbl-cap: \"{cap}\"\n"
        "#| echo: false\n"
        "#| warning: false\n"
        f"obj <- readRDS(\"{rds_relpath}\")\n"
        "if (inherits(obj, \"gtsummary\")) {\n"
        "  if (knitr::pandoc_to(\"docx\") && requireNamespace(\"flextable\", quietly = TRUE)) "
        "gtsummary::as_flex_table(obj) else gtsummary::as_gt(obj)\n"
        "} else if (is.data.frame(obj)) knitr::kable(obj, digits = 2) else obj\n"
        "```\n"
    )


def figure_block(label: str, png_relpath: str, caption: str, width: str = "100%") -> str:
    if not label.startswith("fig-"):
        raise ValueError("Labels de figura devem começar com 'fig-'")
    return f"![{caption}]({png_relpath}){{#{label} width=\"{width}\"}}\n"


def _yaml_header(meta: dict) -> str:
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000) + "---\n"


def build_qmd(*, title: str, sections: list[tuple[str, str]], lang: str = "pt-BR",
              bibliography: str = "referencias.bib", csl: str = "estilo_alvo.csl",
              abstract: Optional[str] = None, keywords: Optional[list[str]] = None,
              authors: Optional[list[str]] = None, reference_doc: Optional[str] = None,
              formats: Iterable[str] = ("docx", "pdf"), extra_meta: Optional[dict] = None) -> str:
    """Gera o documento Quarto completo com YAML, cross-references e seções em Markdown."""
    fmt: dict = {}
    for f in formats:
        if f == "docx":
            fmt["docx"] = {"toc": False, "number-sections": False}
            if reference_doc:
                fmt["docx"]["reference-doc"] = reference_doc
        elif f == "pdf":
            fmt["pdf"] = {"documentclass": "article", "geometry": ["margin=2.5cm"], "fontsize": "11pt",
                          "linestretch": 1.5, "pdf-engine": "xelatex"}
        elif f == "html":
            fmt["html"] = {"embed-resources": True}
        else:
            fmt[f] = "default"
    meta: dict = {"title": title, "lang": lang}
    if authors:
        meta["author"] = authors
    if abstract:
        meta["abstract"] = abstract
    if keywords:
        meta["keywords"] = keywords
    meta.update({
        "bibliography": bibliography, "csl": csl, "link-citations": True,
        "execute": {"echo": False, "warning": False, "message": False},
        "crossref": ({"fig-prefix": "Figura", "tbl-prefix": "Tabela", "fig-title": "Figura", "tbl-title": "Tabela"}
                     if lang.lower().startswith("pt") else {"fig-prefix": "Figure", "tbl-prefix": "Table"}),
        "format": fmt,
    })
    if extra_meta:
        meta.update(extra_meta)
    body = []
    for heading, content in sections:
        if heading:
            body.append(f"# {heading}\n")
        body.append(content.strip() + "\n")
    refs_title = "Referências" if lang.lower().startswith("pt") else "References"
    body.append(f"# {refs_title}\n\n::: {{#refs}}\n:::\n")
    return _yaml_header(meta) + "\n" + "\n".join(body)


def insert_objects(markdown: str, tables: dict[str, dict], figures: dict[str, dict]) -> str:
    """Substitui marcadores `{{tbl-x}}`/`{{fig-x}}` pelos chunks/blocos determinísticos.

    Objetos referenciados no texto (`@tbl-x`) mas sem marcador são anexados ao fim da seção.
    """
    used: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        label = m.group(1)
        used.add(label)
        if label in tables:
            t = tables[label]
            return "\n" + table_chunk(label, t["rds"], t.get("caption") or label) + "\n"
        if label in figures:
            f = figures[label]
            return "\n" + figure_block(label, f["png"], f.get("caption") or label) + "\n"
        return m.group(0)
    out = re.sub(r"\{\{\s*((?:tbl|fig)-[\w-]+)\s*\}\}", repl, markdown)
    for label in re.findall(r"@((?:tbl|fig)-[\w-]+)", out):
        if label in used:
            continue
        used.add(label)
        if label in tables:
            t = tables[label]
            out += "\n\n" + table_chunk(label, t["rds"], t.get("caption") or label)
        elif label in figures:
            f = figures[label]
            out += "\n\n" + figure_block(label, f["png"], f.get("caption") or label)
    return out


def collect_objects(entrega: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Inventaria tabelas (.rds) e figuras (.png) geradas pela Fase 4 com suas legendas (.json)."""
    tables, figures = {}, {}
    for rds in sorted((entrega / "tabelas").glob("*.rds")):
        meta = {}
        if rds.with_suffix(".json").exists():
            meta = json.loads(rds.with_suffix(".json").read_text(encoding="utf-8"))
        label = meta.get("label") or rds.stem.replace("_", "-")
        tables[label] = {"rds": f"tabelas/{rds.name}", "caption": meta.get("caption") or label,
                         "md": f"tabelas/{rds.stem}.md" if rds.with_suffix(".md").exists() else None}
    for png in sorted((entrega / "figuras").glob("*.png")):
        meta = {}
        if png.with_suffix(".json").exists():
            meta = json.loads(png.with_suffix(".json").read_text(encoding="utf-8"))
        label = meta.get("label") or png.stem.replace("_", "-")
        figures[label] = {"png": f"figuras/{png.name}", "caption": meta.get("caption") or label}
    return tables, figures


# ============================================================================================
# Renderização
# ============================================================================================

def quarto_available() -> bool:
    return find_quarto() is not None


def render(qmd: Path, formats: Iterable[str] = ("docx", "pdf"), timeout_s: int = 900) -> dict[str, str]:
    """Executa `quarto render` para cada formato. Retorna {formato: caminho | 'ERRO: ...'}."""
    if not quarto_available():
        raise QuartoNotFound("Quarto não encontrado no PATH. Instale em https://quarto.org/docs/get-started/")
    qmd = Path(qmd).resolve()
    results = {}
    for fmt in formats:
        proc = run_command([find_quarto(), "render", qmd.name, "--to", fmt], cwd=qmd.parent, timeout=timeout_s)
        out = qmd.with_suffix(f".{fmt}")
        results[fmt] = str(out) if proc.returncode == 0 and out.exists() else f"ERRO: {proc.stderr[-1500:]}"
    return results


def qmd_to_preview_markdown(qmd_text: str, entrega: Path) -> str:
    """Troca chunks de tabela pelas prévias .md (para o fallback via Pandoc, sem executar R)."""
    def repl(m: re.Match[str]) -> str:
        chunk = m.group(0)
        label = re.search(r"#\|\s*label:\s*(tbl-[\w-]+)", chunk)
        cap = re.search(r'#\|\s*tbl-cap:\s*"([^"]*)"', chunk)
        rds = re.search(r'readRDS\("([^"]+)"\)', chunk)
        if not (label and rds):
            return ""
        md_path = entrega / Path(rds.group(1)).with_suffix(".md")
        table_md = md_path.read_text(encoding="utf-8") if md_path.exists() else "_(prévia indisponível)_"
        caption = cap.group(1) if cap else label.group(1)
        return f"\n{table_md.strip()}\n\n: {caption} {{#{label.group(1)}}}\n"
    return re.sub(r"```\{r\}.*?```", repl, qmd_text, flags=re.S)


def render_with_pandoc_fallback(qmd: Path, fmt: str = "docx") -> str:
    """Prévia sem Quarto: Pandoc + citeproc. Numeração de cross-refs fica a cargo do Quarto real."""
    pandoc = find_pandoc()
    if pandoc is None:
        raise QuartoNotFound("Nem Quarto nem Pandoc encontrados.")
    qmd = Path(qmd).resolve()
    md = qmd_to_preview_markdown(qmd.read_text(encoding="utf-8"), qmd.parent)
    md_path = qmd.with_name(qmd.stem + "_previa.md")
    md_path.write_text(md, encoding="utf-8")
    out = qmd.with_name(f"{qmd.stem}_previa.{fmt}")
    proc = run_command([pandoc, md_path.name, "--citeproc", "-o", out.name, "--resource-path", "."],
                       cwd=qmd.parent, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc falhou: {proc.stderr[-1500:]}")
    return str(out)


def render_project(entrega: Path, formats: Iterable[str] = ("docx", "pdf")) -> dict[str, dict[str, str]]:
    """Renderiza artigo_final.qmd e material_suplementar.qmd (Quarto; fallback Pandoc se ausente)."""
    results: dict[str, dict[str, str]] = {}
    for name in ("artigo_final.qmd", "material_suplementar.qmd"):
        qmd = Path(entrega) / name
        if not qmd.exists():
            continue
        if quarto_available():
            results[name] = render(qmd, formats)
        else:
            res = {}
            for fmt in formats:
                if fmt == "pdf":
                    res[fmt] = "PULADO: PDF exige Quarto + LaTeX (instale Quarto e rode `quarto install tinytex`)"
                    continue
                try:
                    res[fmt] = render_with_pandoc_fallback(qmd, fmt) + " (prévia Pandoc — instale Quarto para a versão final)"
                except Exception as exc:  # noqa: BLE001 — relatar e seguir
                    res[fmt] = f"ERRO: {exc}"
            results[name] = res
    return results


# ============================================================================================
# Kit de Submissão
# ============================================================================================

def number_paragraphs(qmd_text: str) -> list[dict[str, str]]:
    """Indexa o manuscrito como [{id: 'Métodos §3', secao, texto}] para o mapeamento do checklist."""
    body = re.sub(r"^---\n.*?\n---\n", "", qmd_text, count=1, flags=re.S)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    section = "Preâmbulo"
    counter = 0
    out = []
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block or block.startswith(":::"):
            continue
        h = re.match(r"^(#+)\s+(.*)$", block)
        if h:
            section, counter = h.group(2).strip(), 0
            continue
        if block.startswith("!["):
            continue
        counter += 1
        out.append({"id": f"{section} §{counter}", "secao": section, "texto": block})
    return out


def load_checklist(name: str) -> list[dict[str, str]]:
    """Lê config/checklists/<NAME>.md no formato `- **item** [Seção]: descrição`."""
    path = CONFIG_DIR / "checklists" / f"{name.upper()}.md"
    if not path.exists():
        raise FileNotFoundError(f"Checklist não encontrado: {path}")
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^- \*\*([\w.]+)\*\*\s*\[([^\]]+)\]:\s*(.+)$", line.strip())
        if m:
            items.append({"item": m.group(1), "secao": m.group(2), "descricao": m.group(3)})
    return items


def render_checklist_markdown(name: str, mapping: list[dict[str, str]]) -> str:
    lines = [f"# Checklist {name.replace('_', '-')} preenchido", "",
             "| Item | Recomendação | Local no manuscrito | Status |", "|---|---|---|---|"]
    for m in mapping:
        desc = m.get("descricao", "").replace("|", "/")
        loc = (m.get("local") or "—").replace("|", "/")
        lines.append(f"| {m['item']} | {desc} | {loc} | {m.get('status', 'pendente')} |")
    return "\n".join(lines) + "\n"
