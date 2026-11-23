"""Buscador assíncrono paralelo de literatura (OpenAlex, EuropePMC/PubMed, Crossref, Semantic Scholar).

- `httpx.AsyncClient` + `asyncio.gather` para consultar as quatro fontes simultaneamente;
- rate-limit por host (intervalo mínimo entre requisições) e backoff exponencial com jitter em 429/5xx;
- header `User-Agent` com `mailto:` para o "polite pool" de OpenAlex/Crossref;
- normalização para `Paper`, deduplicação por DOI (ou impressão digital do título);
- verificação de DOI e retratação (Crossref `updated-by`/`update-to` + OpenAlex `is_retracted`);
- texto completo Open Access via EuropePMC (fullTextXML) quando houver PMCID;
- geração de BibTeX com citekeys estáveis.
"""

from __future__ import annotations

import asyncio
import html
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

import httpx

from ..core.schemas import Paper, normalize_doi

OPENALEX = "https://api.openalex.org"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CROSSREF = "https://api.crossref.org"
S2 = "https://api.semanticscholar.org/graph/v1"
SOURCES = ("openalex", "europepmc", "crossref", "semanticscholar")


def polite_user_agent() -> str:
    email = os.environ.get("POLITE_EMAIL", "").strip()
    base = "SciForge/2.0 (+https://github.com/; scientific literature review)"
    return f"{base} mailto:{email}" if email else base


class RateLimiter:
    """Garante um intervalo mínimo entre requisições ao mesmo host (seguro para corrotinas)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


@dataclass
class SwarmConfig:
    per_source_max: int = 40
    max_retries: int = 5
    backoff_base_s: float = 1.5
    rate_limits_rps: dict[str, float] = field(default_factory=lambda: {
        "openalex": 8, "europepmc": 5, "crossref": 5, "semanticscholar": 0.9})
    timeout_s: float = 30.0
    fulltext_max_chars: int = 12000

    @classmethod
    def from_settings(cls, settings: dict) -> "SwarmConfig":
        lit = settings.get("literature", {}) or {}
        cfg = cls()
        cfg.per_source_max = int(lit.get("per_source_max", cfg.per_source_max))
        cfg.max_retries = int(lit.get("max_retries", cfg.max_retries))
        cfg.backoff_base_s = float(lit.get("backoff_base_s", cfg.backoff_base_s))
        cfg.rate_limits_rps.update(lit.get("rate_limits_rps") or {})
        cfg.fulltext_max_chars = int(lit.get("fulltext_max_chars", cfg.fulltext_max_chars))
        return cfg


def _strip_tags(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _openalex_abstract(inv: Optional[dict[str, list[int]]]) -> Optional[str]:
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def title_fingerprint(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)[:120]


class LiteratureSwarm:
    def __init__(self, config: SwarmConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = config or SwarmConfig()
        self.transport = transport
        self.limiters = {s: RateLimiter(self.cfg.rate_limits_rps.get(s, 2)) for s in SOURCES}
        self.limiters.setdefault("unpaywall", RateLimiter(5))
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "LiteratureSwarm":
        headers = {"User-Agent": polite_user_agent(), "Accept": "application/json"}
        self._client = httpx.AsyncClient(timeout=self.cfg.timeout_s, headers=headers, transport=self.transport,
                                         follow_redirects=True)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Use `async with LiteratureSwarm(...) as swarm:`")
        return self._client

    # ------------------------------------------------------------------ HTTP com backoff
    async def _get(self, source: str, url: str, params: dict | None = None, headers: dict | None = None,
                   expect_json: bool = True) -> Any:
        delay = self.cfg.backoff_base_s
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            await self.limiters[source].wait()
            try:
                resp = await self.client.get(url, params=params, headers=headers)
                if resp.status_code == 404:
                    return None
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    last = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    if attempt < self.cfg.max_retries:
                        await asyncio.sleep(wait + random.uniform(0, 0.25 * wait))
                        delay *= 2
                        continue
                    break
                resp.raise_for_status()
                return resp.json() if expect_json else resp.text
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if attempt < self.cfg.max_retries:
                    await asyncio.sleep(delay + random.uniform(0, 0.25 * delay))
                    delay *= 2
        raise RuntimeError(f"{source}: falha após {self.cfg.max_retries + 1} tentativas ({last})")

    # ------------------------------------------------------------------ fontes
    async def search_openalex(self, query: str, from_year: int | None, limit: int) -> list[Paper]:
        params: dict[str, Any] = {"search": query, "per-page": min(limit, 200), "sort": "relevance_score:desc"}
        filters = ["type:article|review"]
        if from_year:
            filters.append(f"from_publication_date:{from_year}-01-01")
        params["filter"] = ",".join(filters)
        email = os.environ.get("POLITE_EMAIL")
        if email:
            params["mailto"] = email
        data = await self._get("openalex", f"{OPENALEX}/works", params)
        out = []
        for w in (data or {}).get("results", [])[:limit]:
            ids = w.get("ids") or {}
            out.append(Paper(
                titulo=w.get("display_name") or w.get("title") or "",
                doi=w.get("doi"), ano=w.get("publication_year"),
                autores=[a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")],
                revista=((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                resumo=_openalex_abstract(w.get("abstract_inverted_index")),
                citacoes=w.get("cited_by_count"), fontes=["openalex"],
                pmid=(ids.get("pmid") or "").rsplit("/", 1)[-1] or None,
                pmcid=(ids.get("pmcid") or "").rsplit("/", 1)[-1] or None,
                open_access=bool((w.get("open_access") or {}).get("is_oa")),
                retratado=bool(w.get("is_retracted")), tipo=w.get("type"), url=w.get("id")))
        return out

    async def search_europepmc(self, query: str, from_year: int | None, limit: int) -> list[Paper]:
        q = query if not from_year else f"({query}) AND PUB_YEAR:[{from_year} TO {date.today().year}]"
        params = {"query": q, "format": "json", "resultType": "core", "pageSize": min(limit, 100)}
        data = await self._get("europepmc", f"{EUROPEPMC}/search", params)
        out = []
        for r in ((data or {}).get("resultList") or {}).get("result", [])[:limit]:
            authors = [a.get("fullName", "") for a in ((r.get("authorList") or {}).get("author") or [])]
            pub_types = ((r.get("pubTypeList") or {}).get("pubType") or [])
            out.append(Paper(
                titulo=_strip_tags(r.get("title")) or "", doi=r.get("doi"),
                ano=int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
                autores=[a for a in authors if a], revista=(r.get("journalInfo") or {}).get("journal", {}).get("title"),
                resumo=_strip_tags(r.get("abstractText")), citacoes=r.get("citedByCount"), fontes=["europepmc"],
                pmid=r.get("pmid"), pmcid=r.get("pmcid"), open_access=r.get("isOpenAccess") == "Y",
                retratado=any("retract" in str(t).lower() for t in pub_types),
                tipo=",".join(map(str, pub_types)) or None))
        return out

    async def search_crossref(self, query: str, from_year: int | None, limit: int) -> list[Paper]:
        params: dict[str, Any] = {"query.bibliographic": query, "rows": min(limit, 100),
                                  "select": "DOI,title,author,issued,container-title,abstract,"
                                            "is-referenced-by-count,type,update-to,relation"}
        flt = ["type:journal-article"]
        if from_year:
            flt.append(f"from-pub-date:{from_year}-01-01")
        params["filter"] = ",".join(flt)
        email = os.environ.get("POLITE_EMAIL")
        if email:
            params["mailto"] = email
        data = await self._get("crossref", f"{CROSSREF}/works", params)
        out = []
        for it in ((data or {}).get("message") or {}).get("items", [])[:limit]:
            year = None
            parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
            if parts and parts[0]:
                year = int(parts[0])
            out.append(Paper(
                titulo=(it.get("title") or [""])[0], doi=it.get("DOI"), ano=year,
                autores=[f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])],
                revista=(it.get("container-title") or [None])[0], resumo=_strip_tags(it.get("abstract")),
                citacoes=it.get("is-referenced-by-count"), fontes=["crossref"], tipo=it.get("type")))
        return out

    async def search_semantic_scholar(self, query: str, from_year: int | None, limit: int) -> list[Paper]:
        params: dict[str, Any] = {"query": query, "limit": min(limit, 100),
                                  "fields": "title,year,authors,venue,abstract,citationCount,externalIds,"
                                            "isOpenAccess,publicationTypes"}
        if from_year:
            params["year"] = f"{from_year}-"
        headers = {}
        key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            headers["x-api-key"] = key
        data = await self._get("semanticscholar", f"{S2}/paper/search", params, headers=headers)
        out = []
        for p in (data or {}).get("data", [])[:limit]:
            ext = p.get("externalIds") or {}
            out.append(Paper(
                titulo=p.get("title") or "", doi=ext.get("DOI"), ano=p.get("year"),
                autores=[a.get("name", "") for a in p.get("authors", [])], revista=p.get("venue") or None,
                resumo=p.get("abstract"), citacoes=p.get("citationCount"), fontes=["semanticscholar"],
                pmid=ext.get("PubMed"), pmcid=(f"PMC{ext['PubMedCentral']}" if ext.get("PubMedCentral") else None),
                open_access=bool(p.get("isOpenAccess")),
                tipo=",".join(p.get("publicationTypes") or []) or None))
        return out

    async def search_all(self, query: str, years_back: int | None = None, max_results: int | None = None,
                         sources: Iterable[str] = SOURCES, agent: str | None = None) -> tuple[list[Paper], dict[str, str]]:
        """Consulta todas as fontes em paralelo. Retorna (papers deduplicados, erros por fonte)."""
        limit = max_results or self.cfg.per_source_max
        from_year = date.today().year - years_back if years_back else None
        funcs = {"openalex": self.search_openalex, "europepmc": self.search_europepmc,
                 "crossref": self.search_crossref, "semanticscholar": self.search_semantic_scholar}
        chosen = [s for s in sources if s in funcs]
        results = await asyncio.gather(*(funcs[s](query, from_year, limit) for s in chosen), return_exceptions=True)
        papers: list[Paper] = []
        errors: dict[str, str] = {}
        for src, res in zip(chosen, results):
            if isinstance(res, BaseException):
                errors[src] = str(res)
                continue
            papers.extend(res)
        if agent:
            for p in papers:
                p.agentes = [agent]
        return deduplicate(papers), errors

    # ------------------------------------------------------------------ verificação
    async def verify_doi(self, doi: str) -> dict[str, Any]:
        """Confere existência do DOI e status de retratação/correção."""
        doi_n = normalize_doi(doi)
        info: dict[str, Any] = {"doi": doi_n, "existe": False, "status_retratacao": "nao_verificado",
                                "titulo": None, "ano": None, "detalhes": []}
        if not doi_n:
            return info
        cr, oa = await asyncio.gather(
            self._get("crossref", f"{CROSSREF}/works/{doi_n}"),
            self._get("openalex", f"{OPENALEX}/works/doi:{doi_n}"),
            return_exceptions=True)
        status = "ok"
        if isinstance(cr, dict) and cr.get("message"):
            msg = cr["message"]
            info["existe"] = True
            info["titulo"] = (msg.get("title") or [None])[0]
            parts = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0]
            info["ano"] = parts[0] if parts else None
            info["autores"] = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in msg.get("author", [])]
            info["revista"] = (msg.get("container-title") or [None])[0]
            for upd in (msg.get("updated-by") or []) + (msg.get("update-to") or []):
                kind = str(upd.get("type", "")).lower()
                info["detalhes"].append(f"Crossref update: {kind}")
                if "retract" in kind or "withdraw" in kind:
                    status = "retratado"
                elif "concern" in kind and status != "retratado":
                    status = "expressao_de_preocupacao"
                elif "correct" in kind and status == "ok":
                    status = "corrigido"
            rel = msg.get("relation") or {}
            if "is-retracted-by" in rel:
                status = "retratado"
        if isinstance(oa, dict) and oa:
            info["existe"] = True
            info["titulo"] = info["titulo"] or oa.get("display_name")
            info["ano"] = info["ano"] or oa.get("publication_year")
            if oa.get("is_retracted"):
                status = "retratado"
                info["detalhes"].append("OpenAlex: is_retracted=true")
        info["status_retratacao"] = status if info["existe"] else "nao_verificado"
        return info

    async def resolve_title(self, title: str) -> Optional[str]:
        """Encontra o DOI mais provável para um título (Crossref)."""
        data = await self._get("crossref", f"{CROSSREF}/works",
                               {"query.bibliographic": title, "rows": 3, "select": "DOI,title"})
        target = title_fingerprint(title)
        for it in ((data or {}).get("message") or {}).get("items", []):
            cand = title_fingerprint((it.get("title") or [""])[0])
            if cand and (cand in target or target in cand or _similar(cand, target) > 0.85):
                return normalize_doi(it.get("DOI"))
        return None

    async def verify_reference(self, doi_or_title: str) -> dict[str, Any]:
        doi = normalize_doi(doi_or_title) if re.match(r"^(https?://(dx\.)?doi\.org/|doi:)?10\.\d{4,}/", doi_or_title.strip(), re.I) else None
        if doi is None:
            doi = await self.resolve_title(doi_or_title)
            if doi is None:
                return {"consulta": doi_or_title, "existe": False, "status_retratacao": "nao_verificado",
                        "detalhes": ["Título não localizado no Crossref"]}
        info = await self.verify_doi(doi)
        info["consulta"] = doi_or_title
        return info

    async def fetch_fulltext(self, paper: Paper) -> Optional[str]:
        """Texto completo Open Access (EuropePMC fullTextXML) truncado para o fichamento."""
        if not paper.pmcid:
            return None
        try:
            xml = await self._get("europepmc", f"{EUROPEPMC}/{paper.pmcid}/fullTextXML", expect_json=False)
        except RuntimeError:
            return None
        if not xml:
            return None
        body = re.search(r"<body[^>]*>(.*)</body>", xml, flags=re.S)
        text = _strip_tags(body.group(1) if body else xml) or ""
        return text[: self.cfg.fulltext_max_chars] or None


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = {a[i:i + 3] for i in range(len(a) - 2)}, {b[i:i + 3] for i in range(len(b) - 2)}
    return len(sa & sb) / max(1, len(sa | sb))


def deduplicate(papers: Iterable[Paper]) -> list[Paper]:
    """Funde registros da mesma obra (DOI normalizado, ou título) agregando fontes/agentes/metadados."""
    merged: dict[str, Paper] = {}
    for p in papers:
        if not p.titulo:
            continue
        key = f"doi:{p.doi}" if p.doi else f"t:{title_fingerprint(p.titulo)}"
        alt = f"t:{title_fingerprint(p.titulo)}"
        if key not in merged and alt in merged and p.doi:
            merged[key] = merged.pop(alt)
        if key not in merged:
            merged[key] = p.model_copy(deep=True)
            continue
        cur = merged[key]
        cur.fontes = sorted(set(cur.fontes) | set(p.fontes))
        cur.agentes = sorted(set(cur.agentes) | set(p.agentes))
        for attr in ("doi", "ano", "revista", "pmid", "pmcid", "url", "tipo"):
            if getattr(cur, attr) in (None, "") and getattr(p, attr):
                setattr(cur, attr, getattr(p, attr))
        if (not cur.resumo) or (p.resumo and len(p.resumo) > len(cur.resumo)):
            cur.resumo = p.resumo or cur.resumo
        if not cur.autores and p.autores:
            cur.autores = p.autores
        cur.citacoes = max(cur.citacoes or 0, p.citacoes or 0) or cur.citacoes
        cur.open_access = cur.open_access or p.open_access
        cur.retratado = cur.retratado or p.retratado
    return list(merged.values())


def rank_papers(papers: list[Paper], current_year: int | None = None) -> list[Paper]:
    """Ordena por consenso entre fontes/agentes, citações (log) e recência; retratados vão ao fim."""
    import math
    year = current_year or date.today().year

    def score(p: Paper) -> float:
        rec = 1.0 / (1 + max(0, year - (p.ano or year - 15)) / 5)
        return (len(p.fontes) * 1.5 + len(p.agentes) * 1.0 + math.log1p(p.citacoes or 0) * 0.6
                + rec * 2 + (0.5 if p.resumo else -2) - (100 if p.retratado else 0))
    return sorted(papers, key=score, reverse=True)


# ------------------------------------------------------------------ BibTeX

def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


_STOP = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "with", "o", "a", "os", "as", "de", "da", "do",
         "e", "em", "no", "na", "um", "uma", "between", "among", "from", "by", "is", "are"}


def make_citekey(paper: Paper, existing: set[str]) -> str:
    first = paper.autores[0] if paper.autores else "anon"
    family = _ascii(first.split(",")[0] if "," in first else first.split()[-1] if first.split() else "anon")
    family = re.sub(r"[^A-Za-z]", "", family).lower() or "anon"
    word = next((w for w in re.findall(r"[A-Za-z]+", _ascii(paper.titulo).lower()) if w not in _STOP and len(w) > 2), "x")
    base = f"{family}{paper.ano or 'nd'}{word}"
    key, i = base, 0
    while key in existing:
        i += 1
        key = f"{base}{chr(96 + i) if i <= 26 else i}"
    existing.add(key)
    return key


def _bib_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("&", "\\&").replace("%", "\\%")


def to_bibtex(papers: list[Paper]) -> str:
    existing: set[str] = {p.citekey for p in papers if p.citekey}
    entries = []
    for p in papers:
        if not p.citekey:
            p.citekey = make_citekey(p, existing)
        authors = " and ".join(_bib_escape(a) for a in p.autores[:30]) or "Anonymous"
        fields = [f"  title = {{{{{_bib_escape(p.titulo)}}}}}", f"  author = {{{authors}}}"]
        if p.ano:
            fields.append(f"  year = {{{p.ano}}}")
        if p.revista:
            fields.append(f"  journal = {{{_bib_escape(p.revista)}}}")
        if p.doi:
            fields.append(f"  doi = {{{p.doi}}}")
        if p.pmid:
            fields.append(f"  pmid = {{{p.pmid}}}")
        entries.append(f"@article{{{p.citekey},\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(entries) + "\n"


def parse_bib_keys(bib_text: str) -> set[str]:
    return set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", bib_text or ""))


def filter_bib(bib_text: str, keys: set[str]) -> str:
    """Mantém apenas as entradas cujas citekeys foram efetivamente citadas."""
    chunks = re.split(r"(?=@\w+\s*\{)", bib_text or "")
    kept = []
    for c in chunks:
        m = re.match(r"@\w+\s*\{\s*([^,\s]+)\s*,", c)
        if m and m.group(1) in keys:
            kept.append(c.strip())
    return "\n\n".join(kept) + ("\n" if kept else "")
