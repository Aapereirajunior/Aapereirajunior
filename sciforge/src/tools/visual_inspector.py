"""Inspeção visual: leitura multimodal de gráficos preliminares (Fase 1) e QA de figuras ggplot2 (Trava 6).

O QA combina checagens determinísticas (formato, DPI no chunk pHYs, dimensões mínimas, existência
das versões vetoriais) com uma inspeção multimodal que procura sobreposição de rótulos, legendas
cortadas, fontes pequenas e eixos confusos. A saída é um `FigureQA` com instruções cirúrgicas de
correção para o script 04_tabelas_figuras.R.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Optional

from ..core.config import load_prompt
from ..core.llm_client import LLMClient
from ..core.schemas import FigureQA

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_info(path: Path) -> dict:
    """Lê largura, altura e DPI (pHYs) de um PNG sem dependências externas."""
    data = Path(path).read_bytes()
    if not data.startswith(PNG_SIG):
        raise ValueError(f"{path} não é um PNG válido")
    pos = 8
    info: dict = {"width": None, "height": None, "dpi_x": None, "dpi_y": None}
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            info["width"], info["height"] = struct.unpack(">II", chunk[:8])
        elif ctype == b"pHYs":
            ppx, ppy, unit = struct.unpack(">IIB", chunk[:9])
            if unit == 1:  # pixels por metro
                info["dpi_x"], info["dpi_y"] = round(ppx * 0.0254), round(ppy * 0.0254)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    return info


def write_test_png(path: Path, width: int = 10, height: int = 10, dpi: int = 300) -> Path:
    """Gera um PNG mínimo (branco) com pHYs — útil para testes e fixtures."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    ppm = round(dpi / 0.0254)
    png = (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)) + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)
    return Path(path)


def deterministic_checks(png: Path, min_dpi: int = 300, min_px: int = 1200) -> list[str]:
    problems = []
    try:
        info = png_info(png)
    except ValueError as exc:
        return [str(exc)]
    if info["dpi_x"] is None or info["dpi_x"] < min_dpi - 2:
        problems.append(f"Resolução {info['dpi_x'] or 'desconhecida'} DPI; exigido ≥ {min_dpi} DPI (ggsave(dpi = {min_dpi})).")
    if info["width"] and info["width"] < min_px:
        problems.append(f"Largura de {info['width']} px é pequena para impressão; aumente width/dpi no ggsave.")
    stem = png.with_suffix("")
    if not (stem.with_suffix(".pdf").exists() or stem.with_suffix(".svg").exists()):
        problems.append("Versão vetorial (PDF/SVG) ausente; use sf_save_figure().")
    return problems


async def inspect_figure(png: Path, llm: LLMClient | None, phase: str = "phase4", caption: str | None = None,
                         min_dpi: int = 300) -> FigureQA:
    """QA completo de uma figura (papel `vision`: Gemini em nuvem no padrão; local só em hybrid/strict).

    Recebe apenas o PNG gerado pela Fase 4 (gráfico agregado), nunca dados individuais."""
    det = deterministic_checks(png, min_dpi=min_dpi)
    if llm is None:
        return FigureQA(aprovado=not det, problemas=det,
                        instrucoes_correcao=[f"Corrigir: {p}" for p in det])
    meta_path = png.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    user = (f"Figura: {png.name}\nLegenda prevista: {caption or meta.get('caption') or '(não informada)'}\n"
            f"Checagens determinísticas: {det or 'nenhum problema'}\n\n"
            "Avalie a imagem anexada e responda no formato JSON do contrato.")
    qa: FigureQA = await llm.complete_json(system=load_prompt("04b_figure_qa"), user=user, phase=phase,
                                           schema=FigureQA, role="vision", images=[png], sensitive=True)
    if det:
        qa.aprovado = False
        qa.problemas = det + qa.problemas
        qa.instrucoes_correcao = [f"Corrigir: {p}" for p in det] + qa.instrucoes_correcao
    return qa


async def describe_preliminary_charts(images: list[Path], llm: LLMClient, phase: str = "phase1") -> list[str]:
    """Extrai padrões visuais dos gráficos preliminares do autor (sem apego ao formato original)."""
    if not images:
        return []
    system = load_prompt("01_intake_deconstructor")
    user = ("TAREFA VISUAL: descreva apenas PADRÕES observados nos gráficos preliminares anexados "
            "(tendências, sazonalidade, diferenças entre grupos, outliers, defasagens). Não descreva cores, "
            "estilo ou layout. Responda em JSON: {\"observacoes_visuais_preliminares\": [\"...\"]} com "
            "itens de no máximo 2 frases.\nArquivos: " + ", ".join(p.name for p in images))
    data = await llm.complete_json(system=system, user=user, phase=phase, role="vision", images=images)
    obs: Optional[list] = data.get("observacoes_visuais_preliminares") if isinstance(data, dict) else None
    return [str(o) for o in (obs or [])]
