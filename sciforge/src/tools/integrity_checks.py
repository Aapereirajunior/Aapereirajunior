"""Checagens de integridade (Travas 1, 2 e auditorias da Fase 6).

- Firewall anti-retalhos: acesso ao rascunho só na Fase 1 (`guarded_read`), impressão digital de
  n-gramas do rascunho (hashes, nunca texto) e `DraftFirewall.guard` que barra prompts das Fases 2-6
  contendo trechos do rascunho; checagem de que o inventário não copia frases do rascunho.
- Independência textual: proporção de frases do artigo final recicladas do rascunho (alerta > 10%).
- Anti-plágio: containment de n-gramas contra o corpus de resumos coletados na Fase 2.
- Auditoria numérica: todo número do manuscrito precisa existir em 04_resultados_verificados.json.
- Auditoria de citações: [@citekey] precisa existir no .bib e no Evidence Ledger, não pode estar
  retratado, e a frase precisa ser suportada pelo fichamento.
- Varredura de clichês de LLM e respeito ao glossário obrigatório.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from ..core.schemas import AuditFinding, AuditReport, EvidenceLedger, InventarioSemente, split_sentences


class FirewallViolation(RuntimeError):
    """Conteúdo do rascunho tentou atravessar o firewall anti-retalhos."""


# ============================================================================================
# Normalização / n-gramas
# ============================================================================================

def normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%.,\s]", " ", t)).strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[.,][0-9]+)?", normalize_text(text))


def ngrams(toks: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)} if len(toks) >= n else set()


def _hash_ngram(ng: tuple[str, ...]) -> str:
    return hashlib.sha1(" ".join(ng).encode()).hexdigest()[:16]


def strip_markup(qmd: str) -> str:
    """Remove YAML, chunks de código, citações, cross-refs e marcação para análise textual."""
    text = re.sub(r"^---\n.*?\n---\n", "", qmd or "", count=1, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r":::[^\n]*", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)(\{[^}]*\})?", " ", text)
    text = re.sub(r"\{#[^}]*\}", " ", text)
    text = re.sub(r"^#+\s.*$", " ", text, flags=re.M)
    text = re.sub(r"[*_`>|]", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def strip_citations(text: str) -> str:
    text = re.sub(r"\[[^\]]*@[^\]]*\]", " ", text)
    return re.sub(r"@(tbl|fig|sec|eq)-[\w-]+", " ", text)


# ============================================================================================
# Trava 1 — Firewall anti-retalhos
# ============================================================================================

PHASE_ORDER = {"phase1": 1, "phase2": 2, "phase3": 3, "phase4": 4, "phase5": 5, "phase6": 6}


def is_seed_draft_path(path: Path, semente_dir: Path) -> bool:
    path, semente_dir = Path(path).resolve(), Path(semente_dir).resolve()
    try:
        rel = path.relative_to(semente_dir)
    except ValueError:
        return False
    parts = rel.parts
    return bool(parts) and (parts[0].startswith("rascunho.") or parts[0] == "graficos_preliminares")


def guarded_read(path: Path, phase: str, semente_dir: Path) -> str:
    """Leitura de arquivo com firewall: o rascunho e os gráficos preliminares só abrem na Fase 1."""
    if is_seed_draft_path(path, semente_dir) and phase != "phase1":
        raise FirewallViolation(
            f"{phase} tentou ler {Path(path).name}: apenas phase1_intake pode acessar o rascunho original.")
    return Path(path).read_text(encoding="utf-8")


class DraftFirewall:
    """Guarda hashes de n-gramas do rascunho (nunca o texto) e barra prompts das Fases 2-6 que os contenham.

    Também guarda hashes de n-gramas curtos (`audit_n`) usados pela auditoria de independência textual da
    Fase 6, de modo que nenhuma fase além da 1 precise reabrir o rascunho.
    """

    def __init__(self, hashes: set[str], n: int = 10, max_shared: int = 2,
                 audit_hashes: set[str] | None = None, audit_n: int = 6):
        self.hashes = hashes
        self.n = n
        self.max_shared = max_shared
        self.audit_hashes = audit_hashes or set()
        self.audit_n = audit_n

    @classmethod
    def from_draft(cls, draft_text: str, n: int = 10, max_shared: int = 2, audit_n: int = 6) -> "DraftFirewall":
        toks = tokens(draft_text)
        return cls({_hash_ngram(g) for g in ngrams(toks, n)}, n, max_shared,
                   {_hash_ngram(g) for g in ngrams(toks, audit_n)}, audit_n)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"n": self.n, "max_shared": self.max_shared, "hashes": sorted(self.hashes),
                                          "audit_n": self.audit_n, "audit_hashes": sorted(self.audit_hashes)}),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["DraftFirewall"]:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(set(data["hashes"]), int(data["n"]), int(data["max_shared"]),
                   set(data.get("audit_hashes", [])), int(data.get("audit_n", 6)))

    def shared_count(self, text: str) -> int:
        return sum(1 for g in ngrams(tokens(text), self.n) if _hash_ngram(g) in self.hashes)

    def guard(self, phase: str, prompt: str) -> None:
        if PHASE_ORDER.get(phase, 0) < 2:
            return
        shared = self.shared_count(prompt)
        if shared > self.max_shared:
            raise FirewallViolation(
                f"Firewall anti-retalhos: prompt da {phase} contém {shared} sequências de {self.n} palavras "
                "idênticas ao rascunho original. O conteúdo bruto do rascunho não pode alimentar as Fases 2-6.")


def inventory_copy_check(inventario: InventarioSemente | dict, draft_text: str, n: int = 12) -> list[str]:
    """Lista campos do inventário que reproduzem trechos literais (≥ n palavras) do rascunho."""
    data = inventario.model_dump() if isinstance(inventario, InventarioSemente) else dict(inventario)
    data.pop("schema_dados", None)
    data.pop("referencias_semente", None)
    data.pop("glossario_obrigatorio", None)
    draft_grams = ngrams(tokens(draft_text), n)
    problems = []

    def walk(prefix: str, obj: Any) -> None:
        if isinstance(obj, str):
            if ngrams(tokens(obj), n) & draft_grams:
                problems.append(prefix)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(f"{prefix}[{i}]", v)
    walk("", data)
    return problems


# ============================================================================================
# Independência textual e anti-plágio
# ============================================================================================

def sentence_containment(sentence: str, ref_hashes: set[str], n: int) -> float:
    g = ngrams(tokens(sentence), n)
    if not g:
        return 0.0
    return sum(1 for x in g if _hash_ngram(x) in ref_hashes) / len(g)


def draft_independence_hashed(final_text: str, ref_hashes: set[str], n: int = 6, containment: float = 0.5,
                              max_ratio: float = 0.10) -> AuditReport:
    """Independência textual usando apenas hashes de n-gramas do rascunho (gerados na Fase 1)."""
    body = strip_citations(strip_markup(final_text))
    sentences = [s for s in split_sentences(body) if len(tokens(s)) >= n]
    recycled = [s for s in sentences if sentence_containment(s, ref_hashes, n) >= containment]
    ratio = len(recycled) / len(sentences) if sentences else 0.0
    rep = AuditReport(metricas={"frases_avaliadas": len(sentences), "frases_recicladas_do_rascunho": len(recycled),
                                "proporcao_reciclada_rascunho": round(ratio, 4)})
    if ratio > max_ratio:
        rep.achados.append(AuditFinding(
            tipo="independencia_rascunho", gravidade="alerta",
            mensagem=f"{ratio:.1%} das frases foram recicladas literalmente do rascunho (limite {max_ratio:.0%})."))
    for s in recycled[:20]:
        rep.achados.append(AuditFinding(tipo="independencia_rascunho", gravidade="info",
                                        mensagem="Frase reciclada do rascunho", trecho=s[:300]))
    return rep


def draft_independence(final_text: str, draft_text: str, n: int = 6, containment: float = 0.5,
                       max_ratio: float = 0.10) -> AuditReport:
    """Proporção de frases do texto final recicladas literalmente do rascunho original."""
    ref = {_hash_ngram(g) for g in ngrams(tokens(draft_text), n)}
    return draft_independence_hashed(final_text, ref, n, containment, max_ratio)


def plagiarism_check(final_text: str, corpus: Iterable[dict[str, str]], n: int = 6,
                     alert: float = 0.35) -> AuditReport:
    """Containment de n-gramas de cada frase do manuscrito contra cada resumo do corpus (Fase 2)."""
    body = strip_citations(strip_markup(final_text))
    sentences = [s for s in split_sentences(body) if len(tokens(s)) >= n + 2]
    corpus = list(corpus)
    index: dict[tuple[str, ...], set[int]] = {}
    for i, doc in enumerate(corpus):
        for g in ngrams(tokens(doc.get("texto", "")), n):
            index.setdefault(g, set()).add(i)
    rep = AuditReport()
    flagged = 0
    for s in sentences:
        g = ngrams(tokens(s), n)
        if not g:
            continue
        counts: dict[int, int] = {}
        for gram in g:
            for i in index.get(gram, ()):
                counts[i] = counts.get(i, 0) + 1
        if not counts:
            continue
        best, hits = max(counts.items(), key=lambda kv: kv[1])
        score = hits / len(g)
        if score >= alert:
            flagged += 1
            rep.achados.append(AuditFinding(
                tipo="plagio", gravidade="erro" if score >= 0.6 else "alerta",
                mensagem=f"Sobreposição de {score:.0%} com {corpus[best].get('citekey') or corpus[best].get('doi') or 'documento do corpus'}",
                trecho=s[:300]))
    rep.metricas["frases_com_sobreposicao_corpus"] = flagged
    rep.metricas["frases_avaliadas_plagio"] = len(sentences)
    return rep


# ============================================================================================
# Auditoria numérica
# ============================================================================================

# Sem sinal: hífens costumam ser separadores de intervalo ("1,12-2,20"); a comparação usa valor absoluto.
_NUM_RE = re.compile(r"(?<![\w@.,])\d+(?:[.,]\d+)*(?:\s?%)?(?![\w])")


def flatten_numbers(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            out.append(float(obj))
    elif isinstance(obj, str):
        for m in re.findall(r"-?\d+(?:\.\d+)?", obj):
            out.append(float(m))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(flatten_numbers(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(flatten_numbers(v))
    return out


def _parse_number(raw: str, lang: str) -> tuple[float, int, bool]:
    """Converte '1.234,5' / '1,234.5' / '12,3%' em (valor, casas_decimais, é_percentual)."""
    s = raw.strip().replace("−", "-").replace("–", "-")
    pct = s.endswith("%")
    s = s.rstrip("%").strip().replace(" ", "")
    if lang.lower().startswith("pt"):
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
    else:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
            s = s.replace(",", "")
        elif "," in s and "." not in s:
            s = s.replace(",", ".")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return float(s), decimals, pct


def _matches(value: float, decimals: int, pct: bool, pool: list[float], rel_tol: float) -> bool:
    """Compara no nível de precisão exibido: 37,5% casa com 37.5 ou 0.375 (2 casas a mais), nunca com 0.41."""
    candidates = [(value, decimals), (value / 100, decimals + 2)]
    for target in pool:
        t = abs(target)
        for v, d in candidates:
            if round(t, d) == round(v, d):
                return True
            if v != 0 and abs(t - v) / abs(v) <= rel_tol:
                return True
    return False


def numeric_audit(manuscript: str, results: dict | list, lang: str = "pt-BR", rel_tol: float = 0.005,
                  extra_allowed: Iterable[float] = (), severity: str = "erro",
                  secao: Optional[str] = None) -> AuditReport:
    """Todo número 'resultado' do manuscrito precisa corresponder a um valor do JSON verificado.

    Ignorados: anos (1900-2100), números inteiros pequenos (0-10) usados em contagens textuais,
    o '95' de IC 95%, números dentro de chunks de código, citações e rótulos de cross-ref.
    """
    text = strip_citations(strip_markup(manuscript))
    text = re.sub(r"\b(IC|CI)\s?95\s?%", r"\1", text)
    text = re.sub(r"\b95\s?%\s?(IC|CI|de confiança|confidence)", r"\1", text, flags=re.I)
    pool = flatten_numbers(results) + [float(x) for x in extra_allowed]
    rep = AuditReport()
    checked = unmatched = 0
    for sentence in split_sentences(text):
        for m in _NUM_RE.finditer(sentence):
            raw = m.group(0)
            try:
                value, decimals, pct = _parse_number(raw, lang)
            except ValueError:
                continue
            if not pct and decimals == 0 and (1900 <= value <= 2100 or 0 <= value <= 10):
                continue
            checked += 1
            if not _matches(value, decimals, pct, pool, rel_tol):
                unmatched += 1
                rep.achados.append(AuditFinding(
                    tipo="numerico", gravidade=severity, secao=secao,
                    mensagem=f"Número '{raw.strip()}' não encontrado em 04_resultados_verificados.json",
                    trecho=sentence.strip()[:300]))
    rep.metricas.update({"numeros_verificados": checked, "numeros_sem_correspondencia": unmatched})
    return rep


# ============================================================================================
# Trava 2 — Auditoria de citações contra .bib + Evidence Ledger
# ============================================================================================

_CITE_GROUP = re.compile(r"\[([^\]]*@[^\]]*)\]")
_CITE_KEY = re.compile(r"-?@([A-Za-z0-9_:.#$%&+?<>~/-]+)")
_STOPWORDS = set("""a o as os de da do das dos e em no na nos nas um uma por para com sem que se ao aos à às
entre sobre como mais menos foi foram ser são era eram tem têm ter este esta estes estas esse essa isso
the of and in on for to with by from at is are was were be been this that these those an as or than
its their which who whom has have had not no""".split())


def _content_words(text: str) -> set[str]:
    return {w for w in tokens(text) if w not in _STOPWORDS and (len(w) > 3 or w.replace(".", "").isdigit())}


def _stem(w: str) -> str:
    return w[:6] if len(w) > 6 else w


def support_score(sentence: str, entry_text: str) -> float:
    s = {_stem(w) for w in _content_words(strip_citations(sentence))}
    e = {_stem(w) for w in _content_words(entry_text)}
    if not s:
        return 1.0
    return len(s & e) / len(s)


def extract_citations(manuscript: str) -> list[tuple[str, list[str]]]:
    """Lista (frase, [citekeys]) para toda frase com citação Pandoc."""
    text = strip_markup(manuscript)
    out = []
    for sent in split_sentences(text):
        keys: list[str] = []
        for grp in _CITE_GROUP.findall(sent):
            keys.extend(k for k in _CITE_KEY.findall(grp) if not re.match(r"(tbl|fig|sec|eq)-", k))
        if keys:
            out.append((sent.strip(), keys))
    return out


JudgeFn = Callable[[str, dict], Awaitable[bool]]


async def citation_audit(manuscript: str, bib_keys: set[str], ledger: EvidenceLedger,
                         min_score: float = 0.12, judge: JudgeFn | None = None) -> AuditReport:
    """Verifica existência, retratação e SUPORTE de cada citação.

    O suporte é estimado pela sobreposição lexical entre a frase e o fichamento (achados, medidas,
    população, desenho). Abaixo de `min_score`, um juiz LLM opcional decide; sem juiz, é sinalizado.
    """
    by_key = ledger.by_key()
    rep = AuditReport()
    cited: set[str] = set()
    unsupported = 0
    pairs = extract_citations(manuscript)
    for sentence, keys in pairs:
        for key in keys:
            cited.add(key)
            if key not in bib_keys:
                rep.achados.append(AuditFinding(tipo="citacao", gravidade="erro",
                                                mensagem=f"@{key} não existe no arquivo .bib", trecho=sentence[:300]))
                continue
            entry = by_key.get(key)
            if entry is None:
                rep.achados.append(AuditFinding(tipo="citacao", gravidade="erro",
                                                mensagem=f"@{key} não possui fichamento no Evidence Ledger",
                                                trecho=sentence[:300]))
                continue
            if entry.status_retratacao in ("retratado", "expressao_de_preocupacao"):
                rep.achados.append(AuditFinding(tipo="retratacao", gravidade="erro",
                                                mensagem=f"@{key} tem status '{entry.status_retratacao}'",
                                                trecho=sentence[:300]))
        # Suporte: a frase precisa ser compatível com pelo menos um dos fichamentos citados.
        entries = [by_key[k] for k in keys if k in by_key]
        if not entries:
            continue
        best = max(support_score(sentence, " ".join(
            [e.titulo or "", e.desenho, e.populacao, *e.achados_principais, *e.medidas_efeito, *e.limitacoes]))
            for e in entries)
        if best >= min_score:
            continue
        supported = False
        if judge is not None:
            for e in entries:
                if await judge(sentence, e.model_dump()):
                    supported = True
                    break
        if not supported:
            unsupported += 1
            rep.achados.append(AuditFinding(
                tipo="suporte_evidencia", gravidade="erro",
                mensagem=f"Alegação possivelmente não suportada pelos fichamentos de {', '.join('@' + k for k in keys)} "
                         f"(score {best:.2f})", trecho=sentence[:300]))
    rep.metricas.update({"frases_com_citacao": len(pairs), "citekeys_distintas": len(cited),
                         "alegacoes_sem_suporte": unsupported})
    rep.metricas["_citekeys_citadas"] = sorted(cited)
    return rep


def retraction_report(ledger: EvidenceLedger) -> AuditReport:
    rep = AuditReport()
    for e in ledger.entradas:
        if e.status_retratacao in ("retratado", "expressao_de_preocupacao"):
            rep.achados.append(AuditFinding(tipo="retratacao", gravidade="alerta",
                                            mensagem=f"{e.citekey} ({e.doi}) — {e.status_retratacao}"))
    return rep


# ============================================================================================
# Estilo: clichês de LLM e glossário
# ============================================================================================

BANNED_PHRASES = [
    "no cenário em constante evolução", "em constante evolução", "desempenha um papel crucial",
    "desempenha um papel fundamental", "vale ressaltar que", "vale destacar que", "é importante ressaltar",
    "é importante notar", "em suma", "em última análise", "neste contexto, é", "lança luz sobre",
    "um testemunho de", "abre caminho para", "no mundo atual", "nos dias de hoje", "cada vez mais importante",
    "delve into", "delves into", "delving", "tapestry", "underscore", "underscores", "underscoring",
    "in the ever-evolving", "ever-evolving landscape", "plays a crucial role", "plays a pivotal role",
    "it is worth noting", "it is important to note", "in conclusion,", "a testament to", "paves the way",
    "navigating the complexities", "in today's world", "shed light on", "sheds light on", "multifaceted",
    "intricate interplay", "holistic", "robust and comprehensive", "seamless", "groundbreaking",
]


def cliche_scan(text: str, extra: Iterable[str] = ()) -> AuditReport:
    body = strip_markup(text).lower()
    rep = AuditReport()
    hits = 0
    for phrase in list(BANNED_PHRASES) + list(extra):
        for m in re.finditer(re.escape(phrase.lower()), body):
            hits += 1
            start = max(0, m.start() - 60)
            rep.achados.append(AuditFinding(tipo="cliche", gravidade="alerta",
                                            mensagem=f"Clichê de LLM: '{phrase}'",
                                            trecho=body[start:m.end() + 60]))
    # Padrão mecânico de três adjetivos/termos em série ("X, Y e Z" repetido em excesso)
    triads = re.findall(r"\b\w+, \w+,? (?:e|and) \w+\b", body)
    sentences = max(1, len(split_sentences(body)))
    if len(triads) / sentences > 0.15:
        rep.achados.append(AuditFinding(tipo="cliche", gravidade="alerta",
                                        mensagem=f"Excesso de enumerações triplas ({len(triads)} em {sentences} frases)"))
    passive = len(re.findall(r"\b(foi|foram|é|são|será|serão|was|were|is|are|been)\s+\w+(ado|ada|ados|adas|ido|ida|idos|idas|ed)\b", body))
    rep.metricas.update({"cliches_detectados": hits, "proporcao_voz_passiva": round(passive / sentences, 3)})
    if passive / sentences > 0.5:
        rep.achados.append(AuditFinding(tipo="cliche", gravidade="alerta",
                                        mensagem=f"Voz passiva excessiva ({passive / sentences:.0%} das frases)"))
    return rep


def remove_cliches(text: str) -> str:
    """Remoção determinística de conectivos vazios (último recurso após o Humanizador)."""
    replacements = {
        r"\bvale ressaltar que\s+": "", r"\bvale destacar que\s+": "", r"\bé importante ressaltar que\s+": "",
        r"\bé importante notar que\s+": "", r"\bem suma,\s*": "", r"\bit is worth noting that\s+": "",
        r"\bit is important to note that\s+": "", r"\bdesempenha um papel crucial\b": "é determinante",
        r"\bplays a crucial role\b": "is central", r"\bdelve into\b": "examine", r"\bunderscores\b": "shows",
        r"\bunderscore\b": "show",
    }
    out = text
    for pat, rep in replacements.items():
        out = re.sub(pat, rep, out, flags=re.I)
    # Recapitaliza início de frase após remoções
    return re.sub(r"(^|[.!?]\s+)([a-záéíóúâêôãõç])", lambda m: m.group(1) + m.group(2).upper(), out)


def glossary_check(text: str, glossary: dict[str, str]) -> AuditReport:
    """Garante que siglas/termos do glossário obrigatório aparecem na forma canônica quando usados."""
    rep = AuditReport()
    body = strip_markup(text)
    for term in glossary:
        canon = term.strip()
        if not canon:
            continue
        pattern = re.compile(rf"\b{re.escape(canon)}\b", re.I)
        for m in pattern.finditer(body):
            if m.group(0) != canon and canon.isupper():
                rep.achados.append(AuditFinding(tipo="glossario", gravidade="alerta",
                                                mensagem=f"Sigla '{canon}' grafada como '{m.group(0)}'"))
    return rep


def figure_ref_check(manuscript: str, available_tables: set[str], available_figures: set[str]) -> AuditReport:
    rep = AuditReport()
    refs = set(re.findall(r"@((?:tbl|fig)-[\w-]+)", manuscript))
    defined = set(re.findall(r"#\|\s*label:\s*((?:tbl|fig)-[\w-]+)", manuscript)) | set(
        re.findall(r"\{#((?:tbl|fig)-[\w-]+)", manuscript))
    for r in sorted(refs - defined):
        rep.achados.append(AuditFinding(tipo="figura", gravidade="erro",
                                        mensagem=f"Referência cruzada @{r} sem objeto definido no documento"))
    for lab in sorted(defined):
        pool = available_tables if lab.startswith("tbl-") else available_figures
        if pool and lab not in pool:
            rep.achados.append(AuditFinding(tipo="figura", gravidade="erro",
                                            mensagem=f"{lab} não corresponde a arquivo gerado pela Fase 4"))
    return rep


def render_audit_markdown(report: AuditReport, title: str = "Relatório de Auditoria Final") -> str:
    lines = [f"# {title}", "", f"_Gerado em {report.gerado_em}_", "", "## Métricas", ""]
    for k, v in report.metricas.items():
        if k.startswith("_"):
            continue
        lines.append(f"- **{k}**: {v}")
    sev = {"erro": [], "alerta": [], "info": []}
    for a in report.achados:
        sev[a.gravidade].append(a)
    lines += ["", f"## Resumo: {len(sev['erro'])} erro(s), {len(sev['alerta'])} alerta(s), {len(sev['info'])} nota(s)", ""]
    for g, label in (("erro", "Erros (bloqueantes)"), ("alerta", "Alertas"), ("info", "Notas")):
        if not sev[g]:
            continue
        lines += [f"### {label}", ""]
        for a in sev[g]:
            loc = f" [{a.secao}]" if a.secao else ""
            lines.append(f"- **{a.tipo}**{loc}: {a.mensagem}")
            if a.trecho:
                lines.append(f"  > {a.trecho}")
        lines.append("")
    if not report.achados:
        lines.append("Nenhum problema encontrado.")
    return "\n".join(lines) + "\n"
