"""Contratos Pydantic que trafegam entre as fases.

Esses modelos são a "linguagem comum" do sistema: toda saída de LLM que alimenta outra fase é
validada contra um deles, e qualquer violação interrompe o fluxo antes de contaminar a próxima etapa.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9\"“(])")


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------------------------
# Fase 1 — Inventário da semente
# ---------------------------------------------------------------------------------------------

class CategoryCount(_Base):
    nivel: str
    n: Optional[int] = None          # None quando suprimido (célula pequena < 5)
    suprimido: bool = False


class ColumnProfile(_Base):
    nome: str
    tipo: str
    n_missing: int
    pct_missing: float
    n_unicos: Optional[int] = None
    possivel_identificador: bool = False
    media: Optional[float] = None
    dp: Optional[float] = None
    minimo: Optional[float] = None
    p25: Optional[float] = None
    mediana: Optional[float] = None
    p75: Optional[float] = None
    maximo: Optional[float] = None
    data_min: Optional[str] = None
    data_max: Optional[str] = None
    niveis: list[CategoryCount] = Field(default_factory=list)


class DatasetProfile(_Base):
    arquivo: str
    formato: str
    engine: str
    n_linhas: int
    n_colunas: int
    colunas: list[ColumnProfile]
    avisos: list[str] = Field(default_factory=list)


class InventarioSemente(_Base):
    """Átomos conceituais da semente. Proibido conter parágrafos ou estrutura de seções do rascunho."""

    ideia_nucleo: str
    contexto_aplicacao: dict[str, str] = Field(default_factory=dict)
    hipoteses_brutas: list[str] = Field(default_factory=list)
    observacoes_visuais_preliminares: list[str] = Field(default_factory=list)
    restricoes_e_preferencias_autor: dict[str, Any] = Field(default_factory=dict)
    referencias_semente: list[str] = Field(default_factory=list)
    schema_dados: list[DatasetProfile] = Field(default_factory=list)
    contexto_tacito_local: list[str] = Field(default_factory=list)
    glossario_obrigatorio: dict[str, str] = Field(default_factory=dict)
    gerado_em: str = Field(default_factory=now_iso)

    @field_validator("ideia_nucleo")
    @classmethod
    def _max_tres_frases(cls, v: str) -> str:
        if len(split_sentences(v)) > 3:
            raise ValueError("ideia_nucleo deve ter no máximo 3 frases")
        return v.strip()

    @field_validator("hipoteses_brutas", "observacoes_visuais_preliminares", "contexto_tacito_local")
    @classmethod
    def _itens_atomicos(cls, v: list[str]) -> list[str]:
        for item in v:
            if len(item) > 400 or len(split_sentences(item)) > 2:
                raise ValueError(
                    "Cada item deve ser um átomo conceitual (≤2 frases, ≤400 caracteres); "
                    f"parágrafo detectado: {item[:80]!r}..."
                )
        return [i.strip() for i in v if i.strip()]

    @field_validator("contexto_aplicacao")
    @classmethod
    def _sem_secoes(cls, v: dict[str, str]) -> dict[str, str]:
        proibidas = {"introducao", "introdução", "metodos", "métodos", "resultados", "discussao", "discussão"}
        for k, val in v.items():
            if k.lower().strip() in proibidas:
                raise ValueError(f"contexto_aplicacao não pode replicar seções do rascunho ({k})")
            if len(str(val)) > 400:
                raise ValueError(f"contexto_aplicacao[{k}] longo demais (≤400 caracteres)")
        return v


# ---------------------------------------------------------------------------------------------
# Fase 2 — Literatura e Evidence Ledger
# ---------------------------------------------------------------------------------------------

class Paper(_Base):
    titulo: str
    doi: Optional[str] = None
    ano: Optional[int] = None
    autores: list[str] = Field(default_factory=list)
    revista: Optional[str] = None
    resumo: Optional[str] = None
    citacoes: Optional[int] = None
    fontes: list[str] = Field(default_factory=list)
    agentes: list[str] = Field(default_factory=list)
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    url: Optional[str] = None
    open_access: bool = False
    retratado: bool = False
    tipo: Optional[str] = None
    citekey: Optional[str] = None

    @field_validator("doi")
    @classmethod
    def _normaliza_doi(cls, v: Optional[str]) -> Optional[str]:
        return normalize_doi(v)


def normalize_doi(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = v.strip().lower()
    v = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", v)
    v = v.removeprefix("doi:").strip()
    return v or None


StatusRetratacao = Literal["ok", "retratado", "expressao_de_preocupacao", "corrigido", "nao_verificado"]


class EvidenceEntry(_Base):
    citekey: str
    doi: Optional[str] = None
    ano: Optional[int] = None
    desenho: str = "não informado"
    populacao: str = "não informado"
    achados_principais: list[str] = Field(default_factory=list)
    medidas_efeito: list[str] = Field(default_factory=list)
    limitacoes: list[str] = Field(default_factory=list)
    status_retratacao: StatusRetratacao = "nao_verificado"
    fonte_texto: Literal["resumo", "texto_completo_oa", "metadados"] = "resumo"
    titulo: Optional[str] = None


class EvidenceLedger(_Base):
    gerado_em: str = Field(default_factory=now_iso)
    entradas: list[EvidenceEntry] = Field(default_factory=list)

    def by_key(self) -> dict[str, EvidenceEntry]:
        return {e.citekey: e for e in self.entradas}


# ---------------------------------------------------------------------------------------------
# Fase 3 — Protocolo e Revisor #2
# ---------------------------------------------------------------------------------------------

class Angulo(_Base):
    titulo: str
    pergunta: str
    desenho: str
    justificativa: str
    viabilidade_com_dados: str


class ProtocoloArquiteto(_Base):
    angulos: list[Angulo] = Field(min_length=1, max_length=3)
    angulo_escolhido: int = 0
    checklist: Literal["STROBE", "RECORD", "RECORD_PE", "TRIPOD_AI"] = "STROBE"
    pacotes_r: list[str] = Field(default_factory=list)
    protocolo_markdown: str


class CriticaRevisor(_Base):
    vies: str
    gravidade: Literal["critica", "maior", "menor"]
    descricao: str
    defesa_sugerida: str


class ParecerRevisor(_Base):
    aprovado: bool
    criticas: list[CriticaRevisor] = Field(default_factory=list)
    comentario_geral: str = ""


# ---------------------------------------------------------------------------------------------
# Fase 4 — Contrato de Diagnósticos Estatísticos (Trava 3)
# ---------------------------------------------------------------------------------------------

class Exclusao(_Base):
    criterio: str
    n_removido: int = Field(ge=0)
    justificativa: Optional[str] = None


class FluxoAmostral(_Base):
    n_inicial: int = Field(gt=0)
    exclusoes: list[Exclusao] = Field(default_factory=list)
    n_final: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistente(self) -> "FluxoAmostral":
        esperado = self.n_inicial - sum(e.n_removido for e in self.exclusoes)
        if esperado != self.n_final:
            raise ValueError(
                f"Fluxo amostral inconsistente: n_inicial ({self.n_inicial}) - exclusões "
                f"({self.n_inicial - esperado}) = {esperado}, mas n_final = {self.n_final}. "
                "Perda amostral silenciosa detectada."
            )
        return self


class MissingVariavel(_Base):
    variavel: str
    pct_missing: float = Field(ge=0, le=100)
    metodo: str


class TratamentoMissing(_Base):
    variaveis: list[MissingVariavel] = Field(default_factory=list)
    metodo_geral: str


class DiagnosticoModelo(_Base):
    modelo: str
    converged: bool
    familia: Optional[str] = None
    dispersao: Optional[float] = None
    superdispersao: Optional[bool] = None
    aic: Optional[float] = None
    qaic: Optional[float] = None
    bic: Optional[float] = None
    r2: Optional[float] = None
    auc: Optional[float] = None
    calibracao: Optional[str] = None
    pressupostos: dict[str, Any] = Field(default_factory=dict)
    avisos: list[str] = Field(default_factory=list)

    @field_validator("pressupostos", mode="before")
    @classmethod
    def _lista_vazia_vira_dict(cls, v: Any) -> Any:
        # jsonlite serializa list() vazia do R como []
        return {} if v in ([], None) else v

    @field_validator("avisos", mode="before")
    @classmethod
    def _aviso_escalar(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else (v or [])


class Estimativa(_Base):
    modelo: str
    termo: str
    medida: str                       # OR, RR, HR, IRR, beta, diferença de médias...
    estimativa: float
    ic95_inf: Optional[float] = None
    ic95_sup: Optional[float] = None
    p_valor: Optional[float] = None
    n: Optional[int] = None

    @model_validator(mode="after")
    def _ic_ordenado(self) -> "Estimativa":
        if self.ic95_inf is not None and self.ic95_sup is not None and self.ic95_inf > self.ic95_sup:
            raise ValueError(f"IC 95% invertido em {self.modelo}/{self.termo}")
        return self


class ResultadosVerificados(_Base):
    model_config = ConfigDict(extra="allow")

    fluxo_amostral: FluxoAmostral
    tratamento_missing: TratamentoMissing
    diagnosticos_modelos: list[DiagnosticoModelo] = Field(min_length=1)
    resultados_principais: list[Estimativa] = Field(min_length=1)
    resultados_sensibilidade: list[Estimativa] = Field(default_factory=list)
    descritivos: dict[str, Any] = Field(default_factory=dict)
    tabelas: list[str] = Field(default_factory=list)
    figuras: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Fase 5 — Storyboard
# ---------------------------------------------------------------------------------------------

SECOES_ORDEM_REDACAO = [
    "Metodos", "Resultados", "Discussao", "Conclusao", "Introducao", "Titulo_Resumo", "Material_Suplementar",
]


class ParagrafoPlano(_Base):
    objetivo: str
    mensagem_chave: str
    citekeys: list[str] = Field(default_factory=list)
    resultados_refs: list[str] = Field(default_factory=list)   # caminhos JSON, ex.: resultados_principais[0]
    tabelas: list[str] = Field(default_factory=list)            # labels tbl-...
    figuras: list[str] = Field(default_factory=list)            # labels fig-...


class SecaoPlano(_Base):
    id: Literal["Introducao", "Metodos", "Resultados", "Discussao", "Conclusao", "Titulo_Resumo",
                "Material_Suplementar"]
    titulo: str
    destino: Literal["manuscrito", "suplementar"]
    limite_palavras: int = Field(gt=0)
    paragrafos: list[ParagrafoPlano] = Field(min_length=1)


class Storyboard(_Base):
    titulo_provisorio: str
    fio_condutor: str
    limite_palavras_total: Optional[int] = None
    estrutura_resumo: list[str] = Field(default_factory=list)
    secoes: list[SecaoPlano]
    itens_suplementares: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _secoes_obrigatorias(self) -> "Storyboard":
        ids = {s.id for s in self.secoes}
        faltando = set(SECOES_ORDEM_REDACAO) - ids
        if faltando:
            raise ValueError(f"Storyboard sem seções obrigatórias: {sorted(faltando)}")
        if self.limite_palavras_total:
            total = sum(s.limite_palavras for s in self.secoes
                        if s.destino == "manuscrito" and s.id != "Titulo_Resumo")
            if total > self.limite_palavras_total:
                raise ValueError(
                    f"Soma dos limites das seções ({total}) excede o limite da revista "
                    f"({self.limite_palavras_total})"
                )
        return self

    def secao(self, sid: str) -> SecaoPlano:
        for s in self.secoes:
            if s.id == sid:
                return s
        raise KeyError(sid)


# ---------------------------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------------------------

class AuditFinding(_Base):
    tipo: Literal["numerico", "citacao", "suporte_evidencia", "retratacao", "plagio",
                  "independencia_rascunho", "cliche", "glossario", "figura", "diagnostico", "outro"]
    gravidade: Literal["erro", "alerta", "info"]
    mensagem: str
    trecho: Optional[str] = None
    secao: Optional[str] = None


class AuditReport(_Base):
    gerado_em: str = Field(default_factory=now_iso)
    achados: list[AuditFinding] = Field(default_factory=list)
    metricas: dict[str, Any] = Field(default_factory=dict)

    @property
    def erros(self) -> list[AuditFinding]:
        return [a for a in self.achados if a.gravidade == "erro"]

    def extend(self, other: "AuditReport") -> None:
        self.achados.extend(other.achados)
        self.metricas.update(other.metricas)


class FigureQA(_Base):
    aprovado: bool
    problemas: list[str] = Field(default_factory=list)
    instrucoes_correcao: list[str] = Field(default_factory=list)
