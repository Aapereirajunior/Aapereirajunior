"""Fase 1 — Desconstrução multimodal da semente + Raio-X real do dataset.

ÚNICA fase autorizada a abrir `00_semente/rascunho.*` e `graficos_preliminares/`.
Executa em paralelo: (a) desconstrução do texto em átomos conceituais, (b) leitura visual dos
gráficos preliminares, (c) profiling agregado em R de cada arquivo de dados. Ao final grava
`01_estado/01_inventario_semente.json`, verifica que ele não contém trechos copiados do rascunho e
ATIVA o firewall anti-retalhos (impressão digital de n-gramas) para as Fases 2-6.
"""

from __future__ import annotations

import asyncio
import re
import zipfile
from pathlib import Path

from ..core.platform_utils import find_pdftotext, run_command
from ..core.schemas import InventarioSemente
from ..tools.data_profiler import profile_dataset, profile_to_prompt
from ..tools.integrity_checks import inventory_copy_check
from ..tools.quarto_renderer import parse_journal_rules
from ..tools.visual_inspector import describe_preliminary_charts
from .base import GateBlocked, PhaseAgent, compact_json

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def read_draft_file(path: Path) -> str:
    """Extrai texto de .md/.txt/.pdf/.docx sem dependências obrigatórias."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        import html
        return html.unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()
    if suffix == ".pdf":
        pdftotext = find_pdftotext()
        if pdftotext:
            proc = run_command([pdftotext, "-enc", "UTF-8", "-layout", path, "-"], timeout=120)
            if proc.returncode == 0:
                return proc.stdout
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("Para ler PDF instale poppler-utils (pdftotext) ou `pip install pypdf`.") from exc
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    raise ValueError(f"Formato de rascunho não suportado: {suffix}")


class Phase1Intake(PhaseAgent):
    phase = "phase1"
    title = "Fase 1 — Desconstrução da semente e Raio-X dos dados"

    async def run(self) -> dict:
        ws = self.ctx.ws
        drafts = ws.draft_files()
        if not drafts:
            raise FileNotFoundError(f"Nenhum rascunho encontrado em {ws.semente} (rascunho.md/.txt/.pdf/.docx)")
        draft_text = "\n\n".join(read_draft_file(p) for p in drafts)
        images = sorted(p for p in ws.graficos_preliminares.glob("*") if p.suffix.lower() in IMAGE_EXTS)
        data_files = ws.data_files()
        if not data_files:
            raise FileNotFoundError(f"Nenhum arquivo de dados em {ws.dados} (.parquet/.csv/.rds/.fst)")
        rules = parse_journal_rules(self.ctx.journal_rules_text())

        # (c) profiling em threads (R é síncrono), (a) e (b) como corrotinas — tudo em paralelo
        profiles_task = asyncio.gather(*(asyncio.to_thread(profile_dataset, p) for p in data_files))
        visual_task = self.cached_step("visual", lambda: describe_preliminary_charts(images, self.ctx.llm))
        profiles = await profiles_task
        schema_txt = "\n\n".join(profile_to_prompt(p) for p in profiles)
        text_task = self.cached_step("desconstrucao", lambda: self._deconstruct(draft_text, schema_txt, rules))
        atoms, visual_obs = await asyncio.gather(text_task, visual_task)

        inventario = await self._assemble(atoms, visual_obs, profiles, draft_text, schema_txt, rules)
        self.ctx.write_json(ws.inventario, inventario)
        self.ctx.activate_firewall(draft_text)
        self.ctx.log(f"  ✓ Inventário gravado e firewall anti-retalhos ATIVADO ({ws.firewall_fingerprint.name})")
        return {"inventario": str(ws.inventario), "datasets": [p.arquivo for p in profiles],
                "graficos": len(images), "n_hipoteses": len(inventario.hipoteses_brutas)}

    async def _deconstruct(self, draft_text: str, schema_txt: str, rules) -> dict:
        user = (
            "RASCUNHO DO AUTOR (fonte exclusiva desta fase — NÃO copie frases):\n<<<\n"
            f"{draft_text[:60000]}\n>>>\n\n"
            f"PERFIL AGREGADO DOS DADOS REAIS:\n{schema_txt[:20000]}\n\n"
            f"REGRAS DO PERIÓDICO (resumo): idioma={rules.idioma}, limite_palavras={rules.limite_palavras}, "
            f"estilo={rules.estilo_csl}, revista={rules.nome}\n\n"
            "Produza o JSON de átomos conceituais conforme o contrato do system prompt."
        )
        return await self.ctx.llm.complete_json(system=self.ctx.prompt("01_intake_deconstructor"), user=user,
                                                phase=self.phase, role="default", sensitive=True)

    async def _assemble(self, atoms: dict, visual_obs: list[str], profiles, draft_text: str, schema_txt: str,
                        rules) -> InventarioSemente:
        prefs = dict(atoms.get("restricoes_e_preferencias_autor") or {})
        if rules.nome:
            prefs.setdefault("periodico_alvo", rules.nome)
        if rules.idioma:
            prefs.setdefault("idioma", rules.idioma)
        payload = {
            "ideia_nucleo": atoms.get("ideia_nucleo", ""),
            "contexto_aplicacao": {k: str(v) for k, v in (atoms.get("contexto_aplicacao") or {}).items()},
            "hipoteses_brutas": atoms.get("hipoteses_brutas") or [],
            "observacoes_visuais_preliminares": visual_obs or atoms.get("observacoes_visuais_preliminares") or [],
            "restricoes_e_preferencias_autor": prefs,
            "referencias_semente": atoms.get("referencias_semente") or [],
            "schema_dados": [p.model_dump() for p in profiles],
            "contexto_tacito_local": atoms.get("contexto_tacito_local") or [],
            "glossario_obrigatorio": atoms.get("glossario_obrigatorio") or {},
        }
        for attempt in range(self.ctx.budget.limit("firewall_regen") + 1):
            problems: list[str] = []
            try:
                inv = InventarioSemente.model_validate(payload)
                problems = inventory_copy_check(inv, draft_text)
            except Exception as exc:  # validação Pydantic (parágrafos, >3 frases etc.)
                inv, problems = None, [str(exc)]
            if inv is not None and not problems:
                return inv
            if attempt >= self.ctx.budget.limit("firewall_regen"):
                break
            self.ctx.log(f"  ⚠ Inventário violou o firewall ({problems[:3]}); reescrevendo em átomos...")
            fixed = await self.ctx.llm.complete_json(
                system=self.ctx.prompt("01_intake_deconstructor"),
                user=("O inventário abaixo foi REJEITADO porque copia trechos literais do rascunho ou contém "
                      f"parágrafos/estrutura de seções. Campos problemáticos: {problems}\n"
                      "Reescreva TODOS os campos textuais com suas próprias palavras, como átomos curtos.\n\n"
                      f"{compact_json({k: v for k, v in payload.items() if k != 'schema_dados'})}"),
                phase=self.phase, sensitive=True)
            for key in ("ideia_nucleo", "contexto_aplicacao", "hipoteses_brutas", "contexto_tacito_local",
                        "observacoes_visuais_preliminares", "glossario_obrigatorio", "restricoes_e_preferencias_autor"):
                if key in fixed:
                    payload[key] = fixed[key]
        raise GateBlocked(f"Inventário continua violando o firewall anti-retalhos: {problems}")
