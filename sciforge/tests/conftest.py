"""Fixtures compartilhadas: workspace sintético e provedor LLM falso determinístico."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Permite rodar os testes sem `pip install -e .`: mapeia o pacote `sciforge` para src/.
if importlib.util.find_spec("sciforge") is None:
    spec = importlib.util.spec_from_file_location("sciforge", ROOT / "src" / "__init__.py",
                                                  submodule_search_locations=[str(ROOT / "src")])
    module = importlib.util.module_from_spec(spec)
    sys.modules["sciforge"] = module
    spec.loader.exec_module(module)

from sciforge.core.llm_client import LLMRequest, LLMResponse  # noqa: E402

from sciforge.core.platform_utils import find_pandoc, find_rscript  # noqa: E402

HAS_R = find_rscript() is not None
HAS_PANDOC = find_pandoc() is not None


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Projeto criado a partir do template + fixtures sintéticas."""
    proj = tmp_path / "projeto_teste"
    shutil.copytree(ROOT / "workspace_template", proj)
    shutil.copy(FIXTURES / "rascunho.md", proj / "00_semente" / "rascunho.md")
    shutil.copy(FIXTURES / "regras_revista.txt", proj / "00_semente" / "regras_revista.txt")
    shutil.copy(FIXTURES / "dados_sinteticos.csv", proj / "00_semente" / "dados" / "dados_sinteticos.csv")
    for png in (FIXTURES / "graficos_preliminares").glob("*.png"):
        shutil.copy(png, proj / "00_semente" / "graficos_preliminares" / png.name)
    return proj


RESULTS_FIXTURE = {
    "fluxo_amostral": {"n_inicial": 500, "exclusoes": [
        {"criterio": "PM2.5 ausente", "n_removido": 12, "justificativa": "exposição não estimada"},
        {"criterio": "tabagismo ausente", "n_removido": 45, "justificativa": "confundidor essencial"}],
        "n_final": 443},
    "tratamento_missing": {"variaveis": [{"variavel": "escolaridade", "pct_missing": 10.4, "metodo": "categoria 'ignorado'"}],
                           "metodo_geral": "análise de casos completos para exposição e confundidores"},
    "diagnosticos_modelos": [{"modelo": "logistico_principal", "converged": True, "familia": "binomial",
                              "dispersao": 1.02, "superdispersao": False, "aic": 471.3, "auc": 0.68,
                              "calibracao": "Hosmer-Lemeshow p=0.41", "pressupostos": {}, "avisos": []}],
    "resultados_principais": [{"modelo": "logistico_principal", "termo": "pm25_10", "medida": "OR",
                               "estimativa": 1.57, "ic95_inf": 1.12, "ic95_sup": 2.2, "p_valor": 0.009, "n": 443}],
    "resultados_sensibilidade": [{"modelo": "imputacao_multipla", "termo": "pm25_10", "medida": "OR",
                                  "estimativa": 1.49, "ic95_inf": 1.08, "ic95_sup": 2.05, "p_valor": 0.015, "n": 500}],
    "descritivos": {"idade_mediana": 72, "pct_internacao": 24.0, "n_internacoes": 106},
    "tabelas": ["tbl-caracteristicas"], "figuras": ["fig-efeito"],
}

LEDGER_FIXTURE = {"entradas": [
    {"citekey": "cohen2017estimates", "doi": "10.1016/s0140-6736(17)30505-6", "ano": 2017,
     "titulo": "Estimates and 25-year trends of the global burden of disease attributable to ambient air pollution",
     "desenho": "estimativa de carga global de doença", "populacao": "195 países, 1990-2015",
     "achados_principais": ["A exposição ao material particulado fino ambiental foi um dos principais fatores de risco para mortes e incapacidade no mundo."],
     "medidas_efeito": ["4,2 milhões de mortes atribuíveis ao PM2.5 em 2015"], "limitacoes": ["incerteza nas curvas exposição-resposta"],
     "status_retratacao": "ok", "fonte_texto": "resumo"},
    {"citekey": "di2017air", "doi": "10.1056/nejmoa1702747", "ano": 2017,
     "titulo": "Air pollution and mortality in the Medicare population",
     "desenho": "coorte nacional", "populacao": "beneficiários do Medicare nos EUA, 2000-2012",
     "achados_principais": ["A exposição de longo prazo ao PM2.5 associou-se a maior mortalidade em idosos, mesmo abaixo dos padrões nacionais de qualidade do ar."],
     "medidas_efeito": ["aumento de 7,3% na mortalidade por 10 µg/m³"], "limitacoes": ["erro de medida da exposição"],
     "status_retratacao": "ok", "fonte_texto": "resumo"},
    {"citekey": "fake2020retracted", "doi": "10.9999/retr", "ano": 2020, "titulo": "Retracted study",
     "desenho": "transversal", "populacao": "adultos", "achados_principais": ["Efeito protetor da poluição."],
     "medidas_efeito": [], "limitacoes": [], "status_retratacao": "retratado", "fonte_texto": "resumo"},
]}

BIB_FIXTURE = """@article{cohen2017estimates,
  title = {{Estimates and 25-year trends of the global burden of disease attributable to ambient air pollution}},
  author = {Aaron J Cohen and Michael Brauer},
  year = {2017},
  journal = {The Lancet},
  doi = {10.1016/s0140-6736(17)30505-6}
}

@article{di2017air,
  title = {{Air pollution and mortality in the Medicare population}},
  author = {Qian Di and Yan Wang},
  year = {2017},
  journal = {New England Journal of Medicine},
  doi = {10.1056/nejmoa1702747}
}
"""

SECTIONS_FAKE = {
    "Metodos": ("Conduzimos um estudo transversal com idosos notificados na rede municipal entre 2018 e 2021. "
                "Ajustamos um modelo logístico para internação respiratória segundo PM2.5 em incrementos de "
                "10 µg/m³, com ajuste por idade, sexo, tabagismo e temperatura. As análises usaram R."),
    "Resultados": ("Dos 500 registros elegíveis, 443 compuseram a amostra analítica. "
                   "As características da amostra estão em @tbl-caracteristicas.\n\n{{tbl-caracteristicas}}\n\n"
                   "Cada incremento de 10 µg/m³ de PM2.5 associou-se a maior chance de internação "
                   "(OR 1,57; IC 95% 1,12-2,20; p = 0,009), conforme @fig-efeito.\n\n{{fig-efeito}}"),
    "Discussao": ("A associação entre PM2.5 e internação respiratória em idosos foi positiva e consistente com a "
                  "maior mortalidade em idosos expostos ao PM2.5 descrita em coortes nacionais [@di2017air]. "
                  "A carga global atribuível ao material particulado fino reforça a relevância do tema [@cohen2017estimates]. "
                  "A imputação múltipla produziu estimativa semelhante (OR 1,49; IC 95% 1,08-2,05)."),
    "Conclusao": "Picos semanais de PM2.5 associaram-se a mais internações respiratórias em idosos.",
    "Introducao": ("O material particulado fino figura entre os principais fatores de risco para mortes no mundo [@cohen2017estimates]. "
                   "Faltam estimativas para idosos atendidos na rede municipal do Nordeste. "
                   "Nosso objetivo foi estimar a associação entre PM2.5 e internação respiratória nessa população."),
    "Material_Suplementar": "A análise com imputação múltipla resultou em OR 1,49 (IC 95% 1,08-2,05).",
}


class FakeProvider:
    """Provedor local falso que responde de acordo com o papel identificado no system prompt."""

    name = "fake"
    local = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate(self, model: str, req: LLMRequest) -> LLMResponse:
        system, user = req.system, req.user
        self.calls.append((system[:60], user[:200]))
        text = self._answer(system, user)
        return LLMResponse(text, len(system + user) // 4, len(text) // 4, self.name, model)

    def _answer(self, system: str, user: str) -> str:
        if "Desconstrutor" in system:
            if "TAREFA VISUAL" in user:
                return json.dumps({"observacoes_visuais_preliminares": ["Internações parecem acompanhar o PM2.5 nos meses frios."]})
            return json.dumps({
                "ideia_nucleo": "Exposição semanal a partículas finas pode elevar o risco de internação respiratória em idosos.",
                "contexto_aplicacao": {"cenario": "rede municipal de saúde", "populacao": "idosos de 60 anos ou mais",
                                       "local": "capital do Nordeste", "periodo": "2018-2021"},
                "hipoteses_brutas": ["Maior PM2.5 na semana anterior eleva a chance de internação?",
                                     "O efeito cresce com a idade?"],
                "restricoes_e_preferencias_autor": {"periodico_alvo": "Cadernos de Saúde Pública", "idioma": "pt-BR"},
                "referencias_semente": ["10.1016/S0140-6736(17)30505-6", "10.1056/NEJMoa1702747"],
                "contexto_tacito_local": ["A ficha municipal tornou escolaridade obrigatória em 2019.",
                                          "O distrito DS-VIII surgiu em 2021 e tem poucos registros."],
                "glossario_obrigatorio": {"SIH/SUS": "Sistema de Informações Hospitalares", "DS": "distrito sanitário"},
            })
        if "Arquiteto Narrativo" in system:
            def sec(sid, titulo, destino, limite, **kw):
                return {"id": sid, "titulo": titulo, "destino": destino, "limite_palavras": limite,
                        "paragrafos": [{"objetivo": f"Parágrafo de {titulo}", "mensagem_chave": "mensagem", **kw}]}
            return json.dumps({
                "titulo_provisorio": "PM2.5 e internação respiratória em idosos",
                "fio_condutor": "Lacuna regional → estimativa da associação → implicação para alertas.",
                "estrutura_resumo": ["Objetivo", "Métodos", "Resultados", "Conclusão"],
                "secoes": [
                    sec("Introducao", "Introdução", "manuscrito", 500, citekeys=["cohen2017estimates"]),
                    sec("Metodos", "Métodos", "manuscrito", 900),
                    sec("Resultados", "Resultados", "manuscrito", 700, tabelas=["tbl-caracteristicas"],
                        figuras=["fig-efeito"], resultados_refs=["resultados_principais[0]", "fluxo_amostral.n_final"]),
                    sec("Discussao", "Discussão", "manuscrito", 1000, citekeys=["di2017air", "cohen2017estimates"]),
                    sec("Conclusao", "Conclusão", "manuscrito", 150),
                    sec("Titulo_Resumo", "Título e Resumo", "manuscrito", 250),
                    sec("Material_Suplementar", "Material Suplementar", "suplementar", 500,
                        resultados_refs=["resultados_sensibilidade[0]"]),
                ],
                "itens_suplementares": ["resultados_sensibilidade"],
            })
        if "Redator Científico" in system:
            m = re.search(r"SEÇÃO A REDIGIR: (\w+)", user)
            sid = m.group(1) if m else "Resultados"
            if sid == "Titulo_Resumo" or "titulo, resumo" in user:
                return json.dumps({"titulo": "PM2.5 e internação respiratória em idosos de uma capital do Nordeste",
                                   "resumo": "Objetivo: estimar a associação entre PM2.5 e internação respiratória. "
                                             "Métodos: estudo transversal com 443 idosos. Resultados: OR 1,57 "
                                             "(IC 95% 1,12-2,20). Conclusão: o PM2.5 associou-se a mais internações.",
                                   "palavras_chave": ["poluição do ar", "idosos", "hospitalização"]})
            if "REVISE a seção" in user:
                m2 = re.search(r"REVISE a seção (\w+)", user)
                return SECTIONS_FAKE.get(m2.group(1) if m2 else "Resultados", "")
            return SECTIONS_FAKE[sid]
        if "Humanizador" in system:
            return user.split("TEXTO:\n", 1)[1]
        if "Revisor #2" in system:
            return json.dumps({"aprovado": True, "criticas": [], "comentario_geral": "ok"})
        if "Fichador" in system:
            return json.dumps({"suportada": True, "motivo": "teste"})
        if "Kit de Submissão" in system:
            if "MODO CHECKLIST" in user:
                return json.dumps({"itens": [{"item": "13a", "local": "Resultados §1", "status": "atendido"},
                                             {"item": "4", "local": "Seção Inexistente §9", "status": "atendido"}]})
            return "Prezado(a) Editor(a),\n\nSubmetemos o manuscrito para apreciação.\n\n[Nome do autor correspondente]"
        return "{}"


def fake_settings(extra: dict | None = None) -> dict:
    roles = {r: {"provider": "fake", "model": "fake-1"} for r in
             ("default", "fast", "vision", "writer", "reviewer", "local", "local_vision")}
    fake = {"provider": "fake", "model": "fake-1"}
    phases = {f"phase{i}": {r: dict(fake) for r in ("*", "vision", "fast", "reviewer", "writer")} for i in range(1, 7)}
    base = {"privacy_mode": "cloud", "llm": {"roles": roles, "phases": phases}, "render": {"formats": ["docx"]}}
    if extra:
        base.update(extra)
    return base
