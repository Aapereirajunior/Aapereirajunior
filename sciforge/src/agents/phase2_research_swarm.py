"""Fase 2 — Enxame de pesquisa: 4 agentes investigadores paralelos + Sintetizador.

Agente 1: estado da arte e lacunas (últimos N anos).
Agente 2: padrão-ouro metodológico epidemiológico (revistas de alto impacto).
Agente 3: controvérsias, vieses comuns e limitações.
Agente 4: auditoria das referências semente e checagem de retratações.
Sintetizador: deduplica por DOI, ranqueia, verifica retratações, extrai o Evidence Ledger
(fichamento estruturado por artigo, via resumo ou texto completo OA) e redige 02_estado_da_arte.md.

Esta fase NUNCA lê o rascunho: recebe apenas o inventário de átomos conceituais.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..core.schemas import EvidenceEntry, EvidenceLedger, InventarioSemente, Paper
from ..tools.literature_swarm import LiteratureSwarm, SwarmConfig, deduplicate, rank_papers, to_bibtex
from .base import PhaseAgent, compact_json

AGENTS = {
    "agente1_estado_da_arte": "Estado da arte e lacunas de conhecimento dos últimos anos",
    "agente2_metodologia": "Padrão-ouro metodológico epidemiológico/estatístico em revistas Q1",
    "agente3_controversias": "Controvérsias, vieses comuns, limitações e resultados nulos/contraditórios",
}


def _inventory_brief(inv: InventarioSemente) -> str:
    return compact_json({
        "ideia_nucleo": inv.ideia_nucleo, "contexto_aplicacao": inv.contexto_aplicacao,
        "hipoteses_brutas": inv.hipoteses_brutas, "glossario_obrigatorio": inv.glossario_obrigatorio,
        "variaveis_disponiveis": [c.nome for d in inv.schema_dados for c in d.colunas if not c.possivel_identificador],
    }, 8000)


class Phase2ResearchSwarm(PhaseAgent):
    phase = "phase2"
    title = "Fase 2 — Enxame de pesquisa bibliográfica e Evidence Ledger"

    async def run(self) -> dict:
        inv = self.ctx.inventario()
        lit = self.ctx.settings.get("literature", {})
        cfg = SwarmConfig.from_settings(self.ctx.settings)
        queries = await self.cached_step("queries", lambda: self._plan_queries(inv))

        async with LiteratureSwarm(cfg) as swarm:
            raw = await self.cached_step("busca", lambda: self._run_agents(swarm, inv, queries, lit))
            papers = [Paper.model_validate(p) for p in raw["papers"]]
            seed_audit = raw["auditoria_semente"]

            ranked = rank_papers(papers)
            pool = [p for p in ranked if p.resumo and not p.retratado][: int(lit.get("final_pool_max", 60))]
            # inclui referências semente verificadas e não retratadas, mesmo que fora do ranking
            pool_dois = {p.doi for p in pool if p.doi}
            for ref in seed_audit:
                if ref.get("existe") and ref.get("status_retratacao") == "ok" and ref.get("doi") not in pool_dois:
                    match = next((p for p in papers if p.doi == ref.get("doi")), None)
                    if match is not None:
                        pool.append(match)
            to_bibtex(pool)  # atribui citekeys estáveis
            statuses = await self.cached_step("retratacoes", lambda: self._check_retractions(swarm, pool))
            fulltexts: dict[str, str] = {}
            if lit.get("fetch_open_access_fulltext", True):
                fulltexts = await self.cached_step("textos_oa", lambda: self._fetch_fulltexts(swarm, pool))

        ledger = await self._build_ledger(pool, statuses, fulltexts)
        self.ctx.write_json(self.ctx.ws.evidence_ledger, ledger)
        valid_keys = {e.citekey for e in ledger.entradas if e.status_retratacao not in ("retratado",)}
        final_pool = [p for p in pool if p.citekey in valid_keys]
        self.ctx.write_text(self.ctx.ws.bib_pool, to_bibtex(final_pool))
        with open(self.ctx.ws.corpus, "w", encoding="utf-8") as fh:
            for p in final_pool:
                fh.write(json.dumps({"citekey": p.citekey, "doi": p.doi,
                                     "texto": (p.resumo or "") + " " + fulltexts.get(p.citekey or "", "")},
                                    ensure_ascii=False) + "\n")
        sota = await self.cached_step("estado_da_arte", lambda: self._synthesize(inv, ledger, seed_audit, raw["erros"]))
        self.ctx.write_text(self.ctx.ws.estado_da_arte, sota)
        return {"artigos_coletados": len(papers), "pool_final": len(final_pool),
                "fichamentos": len(ledger.entradas), "erros_fontes": raw["erros"],
                "referencias_semente_auditadas": len(seed_audit)}

    async def _plan_queries(self, inv: InventarioSemente) -> dict[str, list[str]]:
        user = (f"INVENTÁRIO (átomos conceituais):\n{_inventory_brief(inv)}\n\n"
                "Gere consultas bibliográficas em INGLÊS (bases internacionais) para cada agente. Responda JSON: "
                '{"agente1_estado_da_arte": ["..."], "agente2_metodologia": ["..."], '
                '"agente3_controversias": ["..."]} com 2 a 4 consultas curtas (3-8 termos) por agente, '
                "sem operadores booleanos complexos.")
        data = await self.ctx.llm.complete_json(system=self.ctx.prompt("02_research_synthesizer"), user=user,
                                                phase=self.phase, role="fast")
        out = {k: [str(q) for q in (data.get(k) or [])][:4] for k in AGENTS}
        fallback = inv.ideia_nucleo[:150]
        for k in AGENTS:
            if not out[k]:
                out[k] = [fallback]
        return out

    async def _run_agents(self, swarm: LiteratureSwarm, inv: InventarioSemente, queries: dict[str, list[str]],
                          lit: dict) -> dict[str, Any]:
        years = int(lit.get("years_back_state_of_art", 5))
        settings_by_agent = {"agente1_estado_da_arte": years, "agente2_metodologia": 15, "agente3_controversias": 12}

        async def agent(name: str) -> tuple[list[Paper], dict[str, str]]:
            results = await asyncio.gather(*(swarm.search_all(q, years_back=settings_by_agent[name], agent=name)
                                             for q in queries[name]))
            papers, errors = [], {}
            for ps, errs in results:
                papers.extend(ps)
                errors.update({f"{name}:{k}": v for k, v in errs.items()})
            self.ctx.log(f"  • {name}: {len(papers)} registros")
            return papers, errors

        async def agent4() -> list[dict]:
            refs = inv.referencias_semente[:40]
            checks = await asyncio.gather(*(swarm.verify_reference(r) for r in refs), return_exceptions=True)
            out = []
            for ref, chk in zip(refs, checks):
                if isinstance(chk, BaseException):
                    out.append({"consulta": ref, "existe": False, "status_retratacao": "nao_verificado",
                                "detalhes": [f"erro: {chk}"]})
                else:
                    out.append(chk)
            self.ctx.log(f"  • agente4_auditoria_semente: {len(out)} referências verificadas")
            return out

        results = await asyncio.gather(*(agent(n) for n in AGENTS), agent4())
        all_papers: list[Paper] = []
        errors: dict[str, str] = {}
        for ps, errs in results[:3]:
            all_papers.extend(ps)
            errors.update(errs)
        seed = results[3]
        # artigos semente verificados entram no corpus para fichamento
        for ref in seed:
            if ref.get("existe") and ref.get("doi"):
                all_papers.append(Paper(titulo=ref.get("titulo") or ref["doi"], doi=ref["doi"], ano=ref.get("ano"),
                                        autores=ref.get("autores") or [], revista=ref.get("revista"),
                                        fontes=["crossref"], agentes=["agente4_auditoria_semente"],
                                        retratado=ref.get("status_retratacao") == "retratado"))
        merged = deduplicate(all_papers)
        return {"papers": [p.model_dump() for p in merged], "auditoria_semente": seed, "erros": errors}

    async def _check_retractions(self, swarm: LiteratureSwarm, pool: list[Paper]) -> dict[str, str]:
        sem = asyncio.Semaphore(8)

        async def one(p: Paper) -> tuple[str, str]:
            if not p.doi:
                return p.citekey or "", "nao_verificado"
            async with sem:
                try:
                    info = await swarm.verify_doi(p.doi)
                    return p.citekey or "", info.get("status_retratacao", "nao_verificado")
                except Exception:  # noqa: BLE001
                    return p.citekey or "", "nao_verificado"
        return dict(await asyncio.gather(*(one(p) for p in pool)))

    async def _fetch_fulltexts(self, swarm: LiteratureSwarm, pool: list[Paper]) -> dict[str, str]:
        sem = asyncio.Semaphore(4)

        async def one(p: Paper) -> tuple[str, str | None]:
            if not (p.open_access and p.pmcid):
                return p.citekey or "", None
            async with sem:
                return p.citekey or "", await swarm.fetch_fulltext(p)
        pairs = await asyncio.gather(*(one(p) for p in pool))
        return {k: v for k, v in pairs if v}

    async def _build_ledger(self, pool: list[Paper], statuses: dict[str, str],
                            fulltexts: dict[str, str]) -> EvidenceLedger:
        entries: list[EvidenceEntry] = []
        batch_size = 6
        system = self.ctx.prompt("02b_evidence_extractor")
        for i in range(0, len(pool), batch_size):
            batch = pool[i:i + batch_size]
            key = f"ledger_lote_{i // batch_size}"

            async def extract(batch=batch) -> list[dict]:
                docs = []
                for p in batch:
                    ft = fulltexts.get(p.citekey or "")
                    docs.append({"citekey": p.citekey, "doi": p.doi, "ano": p.ano, "titulo": p.titulo,
                                 "fonte_texto": "texto_completo_oa" if ft else "resumo",
                                 "texto": (ft or p.resumo or "")[:9000]})
                data = await self.ctx.llm.complete_json(
                    system=system, user="Fiche os documentos abaixo.\n" + json.dumps(docs, ensure_ascii=False),
                    phase=self.phase, role="fast")
                items = data.get("entradas", data) if isinstance(data, dict) else data
                return items if isinstance(items, list) else []
            items = await self.cached_step(key, extract)
            by_key = {it.get("citekey"): it for it in items if isinstance(it, dict)}
            for p in batch:
                it = by_key.get(p.citekey, {})
                entries.append(EvidenceEntry(
                    citekey=p.citekey or "", doi=p.doi, ano=p.ano, titulo=p.titulo,
                    desenho=str(it.get("desenho") or "não informado"),
                    populacao=str(it.get("populacao") or "não informado"),
                    achados_principais=[str(x) for x in it.get("achados_principais") or []],
                    medidas_efeito=[str(x) for x in it.get("medidas_efeito") or []],
                    limitacoes=[str(x) for x in it.get("limitacoes") or []],
                    status_retratacao=statuses.get(p.citekey or "", "retratado" if p.retratado else "nao_verificado"),
                    fonte_texto="texto_completo_oa" if p.citekey in fulltexts else ("resumo" if p.resumo else "metadados"),
                ))
        return EvidenceLedger(entradas=[e for e in entries if e.achados_principais])

    async def _synthesize(self, inv: InventarioSemente, ledger: EvidenceLedger, seed_audit: list[dict],
                          errors: dict[str, str]) -> str:
        user = (f"INVENTÁRIO:\n{_inventory_brief(inv)}\n\nEVIDENCE LEDGER ({len(ledger.entradas)} fichamentos):\n"
                f"{compact_json(ledger, 60000)}\n\nAUDITORIA DAS REFERÊNCIAS SEMENTE:\n{compact_json(seed_audit, 6000)}\n\n"
                f"FONTES COM FALHA: {list(errors)}\n\n"
                "Redija 02_estado_da_arte.md conforme o system prompt, citando SOMENTE citekeys do ledger no formato [@citekey].")
        return await self.ctx.llm.complete(system=self.ctx.prompt("02_research_synthesizer"), user=user,
                                           phase=self.phase, role="default")
