"""Servidor MCP local do SciForge — ferramentas reutilizáveis na IDE (Claude Code, Cursor, VS Code...).

Ferramentas:
  search_literature_swarm(query, years_back, max_results)
  profile_r_dataset(file_path)
  verify_doi_and_retraction(doi_or_title)
  audit_quarto_manuscript(qmd_path, results_json_path, evidence_ledger_path, draft_path)
  render_quarto_submission(project_dir, csl_style, output_formats)

Execução: `sciforge-mcp` (stdio) ou `python -m sciforge.mcp_server.server`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

try:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # SDK MCP 2.x renomeou FastMCP para MCPServer (mesma API de decorators)
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

from ..core.schemas import EvidenceLedger
from ..tools import integrity_checks as ic
from ..tools.data_profiler import profile_dataset
from ..tools.literature_swarm import LiteratureSwarm, parse_bib_keys, rank_papers
from ..tools.quarto_renderer import collect_objects, ensure_csl, render_project

mcp = FastMCP("sciforge")


async def audit_manuscript(qmd_path: Path, results_json_path: Path, evidence_ledger_path: Path,
                           draft_path: Optional[Path] = None, fingerprint_path: Optional[Path] = None,
                           bib_path: Optional[Path] = None, corpus_path: Optional[Path] = None,
                           lang: str = "pt-BR") -> str:
    """Executa as auditorias determinísticas e devolve o relatório em Markdown."""
    qmd_path = Path(qmd_path)
    text = qmd_path.read_text(encoding="utf-8")
    results = json.loads(Path(results_json_path).read_text(encoding="utf-8"))
    ledger = EvidenceLedger.model_validate(json.loads(Path(evidence_ledger_path).read_text(encoding="utf-8")))
    bib_path = Path(bib_path) if bib_path else qmd_path.with_name("referencias.bib")
    bib_keys = parse_bib_keys(bib_path.read_text(encoding="utf-8")) if bib_path.exists() else {e.citekey for e in ledger.entradas}
    report = ic.AuditReport()
    report.extend(ic.numeric_audit(text, results, lang))
    report.extend(await ic.citation_audit(text, bib_keys, ledger))
    if draft_path and Path(draft_path).exists():
        report.extend(ic.draft_independence(text, Path(draft_path).read_text(encoding="utf-8", errors="replace")))
    elif fingerprint_path:
        fw = ic.DraftFirewall.load(Path(fingerprint_path))
        if fw is not None and fw.audit_hashes:
            report.extend(ic.draft_independence_hashed(text, fw.audit_hashes, fw.audit_n))
    if corpus_path and Path(corpus_path).exists():
        corpus = [json.loads(line) for line in Path(corpus_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        report.extend(ic.plagiarism_check(text, corpus))
    report.extend(ic.cliche_scan(text))
    tables, figures = collect_objects(qmd_path.parent)
    report.extend(ic.figure_ref_check(text, set(tables), set(figures)))
    return ic.render_audit_markdown(report)


@mcp.tool()
async def search_literature_swarm(query: str, years_back: int = 5, max_results: int = 20) -> str:
    """Busca paralela em OpenAlex, EuropePMC, Crossref e Semantic Scholar; retorna artigos deduplicados por DOI."""
    async with LiteratureSwarm() as swarm:
        papers, errors = await swarm.search_all(query, years_back=years_back, max_results=max_results)
    ranked = rank_papers(papers)[: max_results * 2]
    return json.dumps({"total": len(papers), "erros": errors,
                       "artigos": [{"titulo": p.titulo, "doi": p.doi, "ano": p.ano, "revista": p.revista,
                                    "autores": p.autores[:5], "citacoes": p.citacoes, "fontes": p.fontes,
                                    "open_access": p.open_access, "retratado": p.retratado,
                                    "resumo": (p.resumo or "")[:600]} for p in ranked]},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def profile_r_dataset(file_path: str) -> str:
    """Raio-X agregado (somente leitura, LGPD) de .parquet/.csv/.rds/.fst via scripts_r/profile_data.R."""
    return profile_dataset(Path(file_path)).model_dump_json(indent=1)


@mcp.tool()
async def verify_doi_and_retraction(doi_or_title: str) -> str:
    """Confere existência de um DOI (ou título) no Crossref/OpenAlex e seu status de retratação/correção."""
    async with LiteratureSwarm() as swarm:
        info = await swarm.verify_reference(doi_or_title)
    return json.dumps(info, ensure_ascii=False, indent=1)


@mcp.tool()
async def audit_quarto_manuscript(qmd_path: str, results_json_path: str, evidence_ledger_path: str,
                                  draft_path: str = "") -> str:
    """Auditoria numérica, de citações (Evidence Ledger), clichês, cross-refs e independência vs rascunho."""
    qmd = Path(qmd_path)
    root = qmd.parent.parent
    return await audit_manuscript(qmd, Path(results_json_path), Path(evidence_ledger_path),
                                  Path(draft_path) if draft_path else None,
                                  fingerprint_path=root / "01_estado" / ".firewall_fingerprint.json",
                                  corpus_path=root / "01_estado" / "02_corpus.jsonl")


@mcp.tool()
def render_quarto_submission(project_dir: str, csl_style: str = "vancouver", output_formats: str = "docx,pdf") -> str:
    """Garante o .csl escolhido e renderiza artigo_final.qmd + material_suplementar.qmd (Quarto)."""
    entrega = Path(project_dir) / "03_entrega"
    csl = ensure_csl(csl_style, entrega)
    res = render_project(entrega, [f.strip() for f in output_formats.split(",") if f.strip()])
    return json.dumps({"csl": str(csl), "resultados": res}, ensure_ascii=False, indent=1)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
