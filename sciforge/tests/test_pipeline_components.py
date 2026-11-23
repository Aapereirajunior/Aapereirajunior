"""Testes dos componentes do SciForge.

Cobertura: profiler R, sanitizador LGPD + análise estática, sandbox R com contrato de diagnósticos e
auto-correção, busca assíncrona (mock + integração opcional), auditoria numérica, Evidence Ledger,
firewall anti-retalhos, geração de .qmd/CSL/fallback Pandoc, disjuntor de custo, privacidade do
roteador LLM e um fluxo ponta a ponta (Fases 1, 5 e 6) com LLM falso determinístico.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from conftest import (BIB_FIXTURE, FIXTURES, HAS_R, LEDGER_FIXTURE, RESULTS_FIXTURE, FakeProvider,
                      fake_settings)
from sciforge.agents.base import PhaseContext
from sciforge.agents.phase4_analyst import validate_results
from sciforge.core.budget_guard import AttemptLimitReached, BudgetExceeded, BudgetGuard
from sciforge.core.config import load_settings
from sciforge.core.llm_client import LLMClient, PrivacyViolation, extract_code_block, extract_json
from sciforge.core.schemas import EvidenceLedger, FluxoAmostral, InventarioSemente
from sciforge.core.state_manager import StateManager
from sciforge.tools import integrity_checks as ic
from sciforge.tools.literature_swarm import (LiteratureSwarm, SwarmConfig, deduplicate, filter_bib,
                                             parse_bib_keys, to_bibtex)
from sciforge.tools.quarto_renderer import (build_qmd, ensure_csl, insert_objects, load_checklist,
                                            number_paragraphs, parse_journal_rules, qmd_to_preview_markdown,
                                            render_with_pandoc_fallback, table_chunk)
from sciforge.tools.r_sandbox import RSandbox, sanitize_output, static_check
from sciforge.tools.visual_inspector import deterministic_checks, png_info, write_test_png

requires_r = pytest.mark.skipif(not HAS_R, reason="Rscript não instalado")


# =============================================================================================
# 1. Profiler R (Raio-X agregado)
# =============================================================================================

@requires_r
def test_profile_data_r_aggregates_and_protects_pii(tmp_path):
    from sciforge.tools.data_profiler import profile_dataset
    src = tmp_path / "dados.csv"
    shutil.copy(FIXTURES / "dados_sinteticos.csv", src)
    mtime = src.stat().st_mtime_ns
    prof = profile_dataset(src)
    assert prof.n_linhas == 500 and prof.n_colunas == 14
    cols = {c.nome: c for c in prof.colunas}
    # identificadores detectados e sem níveis expostos
    for ident in ("cns", "id_registro", "data_nascimento"):
        assert cols[ident].possivel_identificador, ident
        assert cols[ident].niveis == []
    assert cols["data_nascimento"].data_min is None
    # supressão de célula pequena (<5)
    ds8 = next(n for n in cols["distrito_sanitario"].niveis if n.nivel == "DS-VIII")
    assert ds8.suprimido and ds8.n is None
    # missings propositais
    assert 5 < cols["escolaridade"].pct_missing < 20
    assert cols["pm25_media_7d"].media is not None and cols["pm25_media_7d"].pct_missing > 0
    assert cols["data_notificacao"].data_min.startswith("2018")
    assert src.stat().st_mtime_ns == mtime  # somente leitura


@requires_r
def test_profile_data_r_rds_format(tmp_path):
    from sciforge.tools.data_profiler import profile_dataset
    sandbox = RSandbox(tmp_path, tmp_path)
    rds = tmp_path / "base.rds"
    res = sandbox.run_code(f'saveRDS(data.frame(x = c(1.5, 2.5, NA), g = c("a", "b", "b")), "{rds}")',
                           check_static=False)
    assert res.ok, res.feedback()
    prof = profile_dataset(rds)
    assert prof.formato == "rds" and prof.n_linhas == 3


# =============================================================================================
# 2. Sanitizador LGPD e análise estática do código R
# =============================================================================================

def test_sanitizer_masks_pii_but_keeps_statistics():
    log = ("Paciente 123.456.789-09 cns 712 3456 7890 1234 email joao.silva@exemplo.com.br "
           "tel (81) 99876-5432 nasc 03/04/1950 data 1950-04-03 cpf 12345678909\n"
           "OR = 1.57 (IC 95% 1.12-2.20), p = 0.00912345678901, N = 443, AIC 471.3")
    out = sanitize_output(log)
    for leaked in ("123.456.789-09", "joao.silva", "99876-5432", "03/04/1950", "1950-04-03", "12345678909",
                   "7890 1234"):
        assert leaked not in out, leaked
    for kept in ("1.57", "1.12-2.20", "0.00912345678901", "443", "471.3"):
        assert kept in out, kept
    assert "[CPF_MASCARADO]" in out and "[EMAIL_MASCARADO]" in out


def test_static_check_blocks_row_level_output():
    bad = """
dados <- read.csv(file.path(P$dados, "x.csv"))
analitica <- dados %>% filter(idade >= 60)
head(dados)
print(analitica)
glimpse(dados)
View(dados)
analitica
system("rm -rf /")
download.file("http://x", "y")
"""
    reasons = " ".join(v.reason for v in static_check(bad))
    for frag in ("head()", "print()", "glimpse()", "View()", "auto-print", "sistema operacional", "downloads"):
        assert frag in reasons, frag


def test_static_check_allows_safe_aggregates():
    good = """
dados <- read.csv("x.csv")
fit_mod <- glm(y ~ x, data = dados, family = binomial)
print(fit_mod)
head(names(dados))
sf_safe_summary(dados)
for (i in 1:3) {
  nrow(dados)
}
# head(dados)  comentário não conta
"""
    assert static_check(good) == []


# =============================================================================================
# 3. Sandbox R + Contrato de Diagnósticos + auto-correção
# =============================================================================================

PIPELINE_R = r'''
suppressPackageStartupMessages({ library(ggplot2) })
source(Sys.getenv("SCIFORGE_R_HELPERS"))
P <- sf_paths()
set.seed(1)
dados <- read.csv(file.path(P$dados, "dados_sinteticos.csv"), stringsAsFactors = FALSE)
dados <- sf_flow_start(dados)
dados <- sf_flow_exclude(dados, !is.na(dados$pm25_media_7d), "PM2.5 ausente", "exposição não estimada")
dados <- sf_flow_exclude(dados, !is.na(dados$tabagismo), "tabagismo ausente", "confundidor essencial")
sf_flow_write(dados)
sf_missing_report(dados, c("escolaridade", "raca_cor"), "categoria ignorado", "casos completos")
dados$pm25_10 <- dados$pm25_media_7d / 10
fit_mod <- glm(internacao_respiratoria ~ pm25_10 + idade + sexo + tabagismo, data = dados, family = binomial)
sf_results_set("diagnosticos_modelos", list(sf_diag_glm(fit_mod, "logistico_principal")))
sf_results_set("resultados_principais", sf_tidy_model(fit_mod, "logistico_principal", termos = "pm25_10", medida = "OR"))
tab_tbl <- aggregate(pm25_media_7d ~ sexo, data = dados, FUN = mean)
sf_save_table(tab_tbl, "tbl-pm25-sexo", caption = "PM2.5 médio por sexo")
p_fig <- ggplot(dados, aes(pm25_media_7d, internacao_respiratoria)) +
  geom_smooth(method = "glm", method.args = list(family = binomial), formula = y ~ x, colour = sf_palette(1)) +
  labs(x = "PM2.5 (µg/m³)", y = "Probabilidade de internação") + sf_theme()
sf_save_figure(p_fig, "fig-efeito", width = 6, height = 4, caption = "Probabilidade de internação por PM2.5")
sf_results_set("tabelas", list("tbl-pm25-sexo"))
sf_results_set("figuras", list("fig-efeito"))
'''


@requires_r
def test_r_sandbox_runs_pipeline_and_contract_validates(workspace):
    (workspace / "01_estado" / "04_resultados_verificados.json").write_text("{}")
    sandbox = RSandbox(workspace)
    res = sandbox.run_code(PIPELINE_R, "pipeline_teste.R")
    assert res.ok, res.feedback()
    results, problems = validate_results(workspace / "01_estado" / "04_resultados_verificados.json", 0.2)
    assert problems == [], problems
    assert results.fluxo_amostral.n_inicial == 500
    assert results.fluxo_amostral.n_final == 500 - sum(e.n_removido for e in results.fluxo_amostral.exclusoes)
    est = results.resultados_principais[0]
    assert est.medida == "OR" and est.ic95_inf < est.estimativa < est.ic95_sup
    assert results.diagnosticos_modelos[0].converged and results.diagnosticos_modelos[0].auc > 0.5
    png = workspace / "03_entrega" / "figuras" / "fig_efeito.png"
    info = png_info(png)
    assert info["dpi_x"] == 300 and info["width"] == 1800
    assert deterministic_checks(png) == []
    assert (workspace / "03_entrega" / "tabelas" / "tbl_pm25_sexo.rds").exists()
    assert "| sexo |" in (workspace / "03_entrega" / "tabelas" / "tbl_pm25_sexo.md").read_text()


@requires_r
def test_r_sandbox_autofix_loop_and_attempt_limit(workspace, tmp_path):
    state = StateManager(tmp_path / "s.sqlite")
    budget = BudgetGuard({"budget": {"limits": {"r_script_max_retries": 4}}}, state)
    sandbox = RSandbox(workspace)
    script = workspace / "02_analise" / "quebrado.R"
    script.write_text('x <- funcao_inexistente(1)\n')
    calls = []

    async def fixer(code, feedback, attempt):
        calls.append(feedback)
        return 'x <- sum(1:10)\ncat("ok", x, "\\n")\n'
    res = asyncio.run(sandbox.run_with_autofix(script, fixer, budget=budget))
    assert res.ok and "ok 55" in res.stdout and len(calls) == 1
    assert "funcao_inexistente" in calls[0]

    script2 = workspace / "02_analise" / "sempre_quebrado.R"
    script2.write_text('stop("erro permanente")\n')

    async def bad_fixer(code, feedback, attempt):
        return 'stop("erro permanente")\n'
    res2 = asyncio.run(sandbox.run_with_autofix(script2, bad_fixer, budget=budget))
    assert not res2.ok
    assert state.attempts("r_script", "sempre_quebrado.R") == 4


def test_contract_blocks_nonconvergence_and_silent_loss(tmp_path):
    with pytest.raises(ValidationError, match="Perda amostral silenciosa"):
        FluxoAmostral(n_inicial=500, exclusoes=[{"criterio": "x", "n_removido": 10}], n_final=400)
    bad = json.loads(json.dumps(RESULTS_FIXTURE))
    bad["diagnosticos_modelos"][0]["converged"] = False
    bad["fluxo_amostral"] = {"n_inicial": 500, "exclusoes": [{"criterio": "sem motivo", "n_removido": 200}], "n_final": 300}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(bad))
    _, problems = validate_results(p, 0.2)
    assert any("NÃO convergiu" in x for x in problems)
    assert any("Perda amostral" in x for x in problems)
    p.write_text(json.dumps(RESULTS_FIXTURE))
    assert validate_results(p, 0.2)[1] == []


# =============================================================================================
# 4. Busca assíncrona de literatura (mock) + integração opcional
# =============================================================================================

def _mock_transport(counter: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        counter[host] = counter.get(host, 0) + 1
        assert "SciForge" in request.headers["user-agent"]
        if host == "api.openalex.org" and path == "/works":
            if counter[host] == 1:  # primeiro acesso: 429 para exercitar o backoff
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"results": [{
                "display_name": "PM2.5 and respiratory admissions in older adults", "doi": "https://doi.org/10.1000/ABC",
                "publication_year": 2023, "authorships": [{"author": {"display_name": "Ana Souza"}}],
                "abstract_inverted_index": {"PM2.5": [0], "increased": [1], "admissions": [2]},
                "cited_by_count": 12, "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123"},
                "open_access": {"is_oa": True}, "is_retracted": False, "type": "article"}]})
        if host == "www.ebi.ac.uk":
            return httpx.Response(200, json={"resultList": {"result": [
                {"title": "PM2.5 and respiratory admissions in older adults", "doi": "10.1000/abc", "pubYear": "2023",
                 "authorList": {"author": [{"fullName": "Souza A"}]}, "abstractText": "<p>Longer abstract about PM2.5 increased admissions.</p>",
                 "citedByCount": 15, "pmid": "999", "isOpenAccess": "Y"},
                {"title": "Heat and mortality", "doi": "10.1000/xyz", "pubYear": "2021", "abstractText": "Heat abstract."}]}})
        if host == "api.crossref.org" and path == "/works":
            return httpx.Response(500)
        if host == "api.crossref.org" and path.startswith("/works/10.1000/retracted"):
            return httpx.Response(200, json={"message": {"title": ["Bad study"], "issued": {"date-parts": [[2019]]},
                                                         "updated-by": [{"type": "retraction"}]}})
        if host == "api.openalex.org" and path.startswith("/works/doi:"):
            return httpx.Response(404)
        if host == "api.semanticscholar.org":
            return httpx.Response(200, json={"data": [{"title": "Air pollution bias review", "year": 2020,
                                                       "authors": [{"name": "Lee K"}], "abstract": "Bias.",
                                                       "externalIds": {"DOI": "10.1000/BIAS"}, "citationCount": 3}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_literature_swarm_parallel_with_mock():
    counter: dict = {}
    cfg = SwarmConfig(max_retries=1, backoff_base_s=0.01,
                      rate_limits_rps={"openalex": 1000, "europepmc": 1000, "crossref": 1000, "semanticscholar": 1000})

    async def go():
        async with LiteratureSwarm(cfg, transport=_mock_transport(counter)) as swarm:
            papers, errors = await swarm.search_all("pm2.5 admissions", years_back=5, agent="agente1")
            retr = await swarm.verify_doi("https://doi.org/10.1000/RETRACTED")
        return papers, errors, retr
    papers, errors, retr = asyncio.run(go())
    by_doi = {p.doi: p for p in papers}
    assert set(by_doi) == {"10.1000/abc", "10.1000/xyz", "10.1000/bias"}  # deduplicado por DOI normalizado
    merged = by_doi["10.1000/abc"]
    assert set(merged.fontes) == {"openalex", "europepmc"} and merged.citacoes == 15
    assert "Longer abstract" in merged.resumo and merged.pmcid == "PMC123" and merged.agentes == ["agente1"]
    assert "crossref" in errors  # 500 persistente vira erro reportado, sem derrubar as outras fontes
    assert counter["api.openalex.org"] >= 2  # houve retry após 429
    assert retr["existe"] and retr["status_retratacao"] == "retratado"


def test_bibtex_generation_and_filtering():
    from sciforge.core.schemas import Paper
    papers = deduplicate([Paper(titulo="The effect of heat on health", autores=["Maria da Silva"], ano=2020, doi="10.1/a"),
                          Paper(titulo="Heat effects revisited", autores=["Maria da Silva"], ano=2020, doi="10.1/b")])
    bib = to_bibtex(papers)
    keys = parse_bib_keys(bib)
    assert keys == {"silva2020effect", "silva2020heat"}
    assert parse_bib_keys(filter_bib(bib, {"silva2020heat"})) == {"silva2020heat"}


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("SCIFORGE_RUN_INTEGRATION") != "1", reason="defina SCIFORGE_RUN_INTEGRATION=1")
def test_literature_swarm_integration_real_apis():
    async def go():
        async with LiteratureSwarm(SwarmConfig(per_source_max=5)) as swarm:
            papers, errors = await swarm.search_all("fine particulate matter hospital admissions elderly",
                                                    years_back=5, max_results=5)
            doi = await swarm.verify_doi("10.1056/NEJMoa1702747")
        return papers, errors, doi
    papers, errors, doi = asyncio.run(go())
    assert len(errors) < 4 and len(papers) >= 3
    assert doi["existe"] and doi["status_retratacao"] in ("ok", "corrigido")


# =============================================================================================
# 5. Auditoria numérica
# =============================================================================================

def test_numeric_audit_accepts_verified_and_flags_invented_numbers():
    ok = ("Dos 500 registros, 443 foram analisados. O OR foi 1,57 (IC 95% 1,12-2,20; p = 0,009) em 2021. "
          "A internação ocorreu em 24,0% da amostra e a AUC foi 0,68.")
    rep = ic.numeric_audit(ok, RESULTS_FIXTURE, "pt-BR")
    assert rep.erros == [], [a.mensagem for a in rep.erros]
    bad = "O OR foi 1,83 (IC 95% 1,12-2,20) e 37,5% dos idosos foram internados."
    rep2 = ic.numeric_audit(bad, RESULTS_FIXTURE, "pt-BR")
    flagged = {a.mensagem.split("'")[1] for a in rep2.erros}
    assert flagged == {"1,83", "37,5%"}


def test_numeric_audit_english_and_ignores_code_and_citations():
    text = ("```{r}\nx <- 999.99\n```\nThe OR was 1.57 (95% CI 1.12-2.20) [@smith2019; @lee2020].\n"
            "See @tbl-caracteristicas.")
    rep = ic.numeric_audit(text, RESULTS_FIXTURE, "en")
    assert rep.erros == []


# =============================================================================================
# 6. Evidence Ledger: citações contra .bib + fichamentos
# =============================================================================================

def test_citation_audit_against_ledger():
    ledger = EvidenceLedger.model_validate(LEDGER_FIXTURE)
    bib_keys = parse_bib_keys(BIB_FIXTURE) | {"fake2020retracted"}
    text = (
        "A exposição de longo prazo ao PM2.5 associou-se a maior mortalidade em idosos [@di2017air]. "
        "A poluição causa diabetes tipo 1 em crianças pequenas de Marte [@cohen2017estimates]. "
        "Um estudo sugeriu efeito protetor [@fake2020retracted]. "
        "Outro achado relevante [@inexistente2024]. "
        "Achado sem fichamento [@semficha2021]."
    )
    rep = asyncio.run(ic.citation_audit(text, bib_keys | {"semficha2021"}, ledger, min_score=0.2))
    msgs = " | ".join(a.mensagem for a in rep.achados)
    assert "@inexistente2024 não existe no arquivo .bib" in msgs
    assert "@semficha2021 não possui fichamento" in msgs
    assert "@fake2020retracted tem status 'retratado'" in msgs
    unsupported = [a for a in rep.achados if a.tipo == "suporte_evidencia"]
    assert any("Marte" in (a.trecho or "") for a in unsupported)
    assert not any("mortalidade em idosos" in (a.trecho or "") for a in unsupported)

    async def judge_yes(sentence, entry):
        return True
    rep2 = asyncio.run(ic.citation_audit("Cohen relatou algo vago [@cohen2017estimates].", bib_keys, ledger,
                                         min_score=0.9, judge=judge_yes))
    assert not [a for a in rep2.achados if a.tipo == "suporte_evidencia"]


# =============================================================================================
# 7. Firewall anti-retalhos e independência textual
# =============================================================================================

DRAFT = (FIXTURES / "rascunho.md").read_text(encoding="utf-8")


def test_firewall_guarded_read_and_prompt_guard(workspace, tmp_path):
    draft_path = workspace / "00_semente" / "rascunho.md"
    assert "Poluição" in ic.guarded_read(draft_path, "phase1", workspace / "00_semente")
    with pytest.raises(ic.FirewallViolation):
        ic.guarded_read(draft_path, "phase3", workspace / "00_semente")
    with pytest.raises(ic.FirewallViolation):
        ic.guarded_read(workspace / "00_semente" / "graficos_preliminares" / "serie_preliminar.png", "phase6",
                        workspace / "00_semente")
    assert ic.guarded_read(workspace / "00_semente" / "regras_revista.txt", "phase5", workspace / "00_semente")

    fw = ic.DraftFirewall.from_draft(DRAFT)
    fw.save(tmp_path / "fp.json")
    fw = ic.DraftFirewall.load(tmp_path / "fp.json")
    assert "Poluição" not in (tmp_path / "fp.json").read_text()  # apenas hashes
    leak = "Contexto:\n" + DRAFT.split("\n\n")[1]
    fw.guard("phase1", leak)  # Fase 1 pode
    with pytest.raises(ic.FirewallViolation):
        fw.guard("phase2", leak)
    fw.guard("phase2", "Estudo sobre PM2.5 e internação respiratória em idosos de 2018 a 2021.")


def test_inventory_rejects_copied_or_paragraph_content():
    copied = {"ideia_nucleo": "Neste trabalho queremos avaliar se a exposição ao material particulado fino aumenta as internações por causas respiratórias entre idosos acompanhados pela rede municipal."}
    assert ic.inventory_copy_check(copied, DRAFT) == ["ideia_nucleo"]
    own = {"ideia_nucleo": "Partículas finas podem elevar internações respiratórias de idosos."}
    assert ic.inventory_copy_check(own, DRAFT) == []
    with pytest.raises(ValidationError):
        InventarioSemente(ideia_nucleo="Frase um. Frase dois. Frase três. Frase quatro.")
    with pytest.raises(ValidationError):
        InventarioSemente(ideia_nucleo="ok.", hipoteses_brutas=["A. B. C. D."])
    with pytest.raises(ValidationError):
        InventarioSemente(ideia_nucleo="ok.", contexto_aplicacao={"Introdução": "texto"})


def test_draft_independence_ratio():
    recycled = ("Usamos a base de notificações da vigilância municipal cruzada com estimativas semanais de PM2.5 "
                "por distrito sanitário. Também achamos que fumantes atuais têm mais internações nesta base.")
    new = " ".join(f"Frase original número {i} sobre o desenho transversal e o ajuste por confundidores." for i in range(10))
    rep = ic.draft_independence(recycled + " " + new, DRAFT)
    assert rep.metricas["frases_recicladas_do_rascunho"] == 2
    assert any(a.tipo == "independencia_rascunho" and a.gravidade == "alerta" for a in rep.achados)  # 2/12 > 10%
    rep_ok = ic.draft_independence(new, DRAFT)
    assert rep_ok.metricas["proporcao_reciclada_rascunho"] == 0
    fw = ic.DraftFirewall.from_draft(DRAFT)
    rep_h = ic.draft_independence_hashed(recycled + " " + new, fw.audit_hashes, fw.audit_n)
    assert rep_h.metricas == rep.metricas


def test_plagiarism_and_cliches():
    corpus = [{"citekey": "x2020", "texto": "Long-term exposure to fine particulate matter was associated with increased mortality among older adults in the national cohort."}]
    text = "Long-term exposure to fine particulate matter was associated with increased mortality among older adults in the national cohort."
    rep = ic.plagiarism_check(text, corpus)
    assert rep.achados and rep.achados[0].gravidade == "erro"
    cl = ic.cliche_scan("Vale ressaltar que a poluição desempenha um papel crucial. This study delves into the tapestry.")
    assert cl.metricas["cliches_detectados"] >= 4
    cleaned = ic.remove_cliches("Vale ressaltar que a poluição desempenha um papel crucial.")
    assert cleaned.startswith("A poluição") and "papel crucial" not in cleaned


# =============================================================================================
# 8. Quarto: .qmd, tabelas determinísticas, CSL, fallback Pandoc, checklist
# =============================================================================================

def test_qmd_generation_with_deterministic_tables(tmp_path):
    tables = {"tbl-caracteristicas": {"rds": "tabelas/tbl_caracteristicas.rds", "caption": "Características"}}
    figures = {"fig-efeito": {"png": "figuras/fig_efeito.png", "caption": "Efeito do PM2.5"}}
    body = insert_objects("Ver @tbl-caracteristicas.\n\n{{tbl-caracteristicas}}\n\nE @fig-efeito.", tables, figures)
    assert '#| label: tbl-caracteristicas' in body and 'readRDS("tabelas/tbl_caracteristicas.rds")' in body
    assert "{#fig-efeito" in body  # anexada automaticamente pois só havia @fig-efeito
    qmd = build_qmd(title="Título", sections=[("Resultados", body)], abstract="Resumo.", keywords=["a"],
                    reference_doc="modelo.docx")
    import yaml
    header = yaml.safe_load(qmd.split("---\n")[1])
    assert header["bibliography"] == "referencias.bib" and header["csl"] == "estilo_alvo.csl"
    assert header["crossref"]["tbl-prefix"] == "Tabela" and header["format"]["docx"]["reference-doc"] == "modelo.docx"
    assert "pdf" in header["format"] and "::: {#refs}" in qmd
    import re
    assert not re.search(r"^\|.*\|\s*$", body, flags=re.M)  # nenhuma célula de tabela transcrita
    with pytest.raises(ValueError):
        table_chunk("tabela1", "x.rds", "cap")


def test_csl_local_and_download(tmp_path):
    van = ensure_csl("vancouver", tmp_path / "a")
    assert "<style" in van.read_text() and van.name == "estilo_alvo.csl"
    assert "Associação Brasileira" in ensure_csl("abnt", tmp_path / "b").read_text()

    def handler(request):
        if request.url.path.endswith("/estilo-teste-sciforge.csl") and "/dependent/" not in request.url.path:
            return httpx.Response(200, text='<?xml version="1.0"?><style xmlns="http://purl.org/net/xbiblio/csl"></style>')
        return httpx.Response(404)
    from sciforge.core import config
    path = ensure_csl("estilo-teste-sciforge", tmp_path / "c", transport=httpx.MockTransport(handler))
    assert "<style" in path.read_text()
    (config.CONFIG_DIR / "csl" / "estilo-teste-sciforge.csl").unlink()


def test_journal_rules_parsing():
    rules = parse_journal_rules((FIXTURES / "regras_revista.txt").read_text(encoding="utf-8"))
    assert rules.limite_palavras == 3500 and rules.limite_resumo == 250 and rules.estilo_csl == "vancouver"
    assert rules.idioma == "pt-BR" and rules.max_tabelas_figuras == 5 and rules.max_referencias == 40
    assert rules.estrutura_resumo == ["Objetivo", "Métodos", "Resultados", "Conclusão"]


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc não instalado")
def test_pandoc_fallback_renders_docx(tmp_path):
    (tmp_path / "tabelas").mkdir()
    (tmp_path / "tabelas" / "tbl_x.md").write_text("| a | b |\n|---|---|\n| 1 | 2 |\n")
    (tmp_path / "referencias.bib").write_text(BIB_FIXTURE)
    ensure_csl("vancouver", tmp_path)
    qmd = build_qmd(title="Teste", sections=[("Resultados", "Texto [@di2017air].\n\n" + table_chunk("tbl-x", "tabelas/tbl_x.rds", "Tab X"))],
                    formats=["docx"])
    (tmp_path / "artigo_final.qmd").write_text(qmd)
    preview = qmd_to_preview_markdown(qmd, tmp_path)
    assert ": Tab X {#tbl-x}" in preview and "readRDS" not in preview
    out = render_with_pandoc_fallback(tmp_path / "artigo_final.qmd")
    assert Path(out).exists() and Path(out).stat().st_size > 1000


def test_checklists_and_paragraph_index():
    for name in ("STROBE", "RECORD", "RECORD_PE", "TRIPOD_AI"):
        items = load_checklist(name)
        assert len(items) >= 10, name
    assert len(load_checklist("STROBE")) == 34
    paras = number_paragraphs("---\ntitle: x\n---\n# Métodos\n\nP1.\n\nP2.\n\n# Resultados\n\nR1.\n")
    assert [p["id"] for p in paras] == ["Métodos §1", "Métodos §2", "Resultados §1"]


# =============================================================================================
# 9. Disjuntor de custo, estado e privacidade do roteador
# =============================================================================================

def test_budget_guard_and_resumable_state(tmp_path):
    st = StateManager(tmp_path / "s.sqlite")
    bg = BudgetGuard({"budget": {"global_max_cost_usd": 1.0, "phases": {"phase2": {"max_calls": 2, "max_tokens": 1000, "max_cost_usd": 0.5}},
                                 "limits": {"reviewer_max_rounds": 2}}}, st)
    bg.check("phase2", 100)
    bg.record("phase2", "p", "m", 300, 200, 0.1)
    bg.record("phase2", "p", "m", 300, 100, 0.1)
    with pytest.raises(BudgetExceeded, match="chamadas"):
        bg.check("phase2", 10)
    bg.record("phase3", "p", "m", 1, 1, 1.0)
    with pytest.raises(BudgetExceeded, match="global"):
        bg.check("phase3")
    assert bg.consume_attempt("reviewer", "phase6") == 1 and bg.consume_attempt("reviewer", "phase6") == 2
    with pytest.raises(AttemptLimitReached):
        bg.consume_attempt("reviewer", "phase6")
    st.save_step("phase6", "secao:Metodos", "texto")
    st.set_phase("phase1", "done")
    st2 = StateManager(tmp_path / "s.sqlite")  # "reinício" do processo
    assert st2.get_step("phase6", "secao:Metodos") == "texto" and st2.next_phase() == "phase2"
    st2.reset_from("phase1")
    assert st2.next_phase() == "phase1"


def test_llm_router_privacy_modes():
    settings = load_settings()
    settings["privacy_mode"] = "hybrid"
    client = LLMClient(settings)
    provider, model = client.resolve("writer", "phase4", sensitive=False, vision=False)
    assert provider.local and provider.name == "ollama"
    provider, _ = client.resolve("vision", "phase4", sensitive=True, vision=True)
    assert provider.name == "ollama"
    provider, _ = client.resolve("writer", "phase6", sensitive=False, vision=False)
    assert provider.name == "gemini"
    settings["privacy_mode"] = "strict"
    settings["llm"]["roles"]["local"] = {"provider": "gemini", "model": "gemini-2.5-pro"}
    with pytest.raises(PrivacyViolation):
        LLMClient(settings).resolve("default", "phase2", sensitive=False, vision=False)


def test_llm_json_and_code_extraction():
    assert extract_json('Claro!\n```json\n{"a": [1, {"b": "}"}]}\n```') == {"a": [1, {"b": "}"}]}
    assert extract_json('texto {"x": 1} fim') == {"x": 1}
    assert extract_code_block("```r\nx <- 1\n```\nfim") == "x <- 1\n"


def test_png_info_and_visual_qa_deterministic(tmp_path):
    png = write_test_png(tmp_path / "f.png", 2000, 1500, 300)
    (tmp_path / "f.pdf").write_bytes(b"%PDF")
    assert png_info(png) == {"width": 2000, "height": 1500, "dpi_x": 300, "dpi_y": 300}
    assert deterministic_checks(png) == []
    low = write_test_png(tmp_path / "g.png", 400, 300, 72)
    probs = deterministic_checks(low)
    assert len(probs) == 3


# =============================================================================================
# 10. Fluxo ponta a ponta com LLM falso: Fase 1 (R real) + Fases 5 e 6
# =============================================================================================

def _ctx(workspace, provider):
    settings = load_settings(workspace, fake_settings())
    client = LLMClient(settings, providers={"fake": provider})
    return PhaseContext.create(workspace, settings_extra=fake_settings(), llm=client, log=lambda m: None)


@requires_r
def test_phase1_builds_inventory_and_activates_firewall(workspace):
    from sciforge.agents.phase1_intake import Phase1Intake
    provider = FakeProvider()
    ctx = _ctx(workspace, provider)
    result = asyncio.run(Phase1Intake(ctx).execute())
    inv = InventarioSemente.model_validate_json((workspace / "01_estado" / "01_inventario_semente.json").read_text())
    assert result["datasets"] == ["dados_sinteticos.csv"] and inv.schema_dados[0].n_linhas == 500
    assert inv.glossario_obrigatorio["SIH/SUS"] and inv.observacoes_visuais_preliminares
    assert (workspace / "01_estado" / ".firewall_fingerprint.json").exists()
    assert ctx.state.phase_status("phase1") == "done"
    # depois da Fase 1, qualquer prompt com trecho do rascunho é barrado
    ctx2 = _ctx(workspace, provider)
    with pytest.raises(ic.FirewallViolation):
        asyncio.run(ctx2.llm.complete(system="x", user=DRAFT, phase="phase3"))


def _seed_downstream_artifacts(ws: Path) -> None:
    est = ws / "01_estado"
    (est / "01_inventario_semente.json").write_text(json.dumps({
        "ideia_nucleo": "PM2.5 e internação em idosos.", "glossario_obrigatorio": {"DS": "distrito sanitário"}}))
    (est / "02_evidence_ledger.json").write_text(json.dumps(LEDGER_FIXTURE))
    (est / "referencias_pool.bib").write_text(BIB_FIXTURE)
    (est / "02_estado_da_arte.md").write_text("# Estado da arte\nSíntese [@di2017air].")
    (est / "02_corpus.jsonl").write_text(json.dumps({"citekey": "di2017air", "texto": "Medicare cohort abstract."}) + "\n")
    (est / "03_protocolo_proposto.md").write_text("# Protocolo\nEstudo transversal; modelo logístico.")
    (est / "03_critica_revisor2.json").write_text(json.dumps({"rodadas": [], "checklist": "STROBE"}))
    (est / "04_resultados_verificados.json").write_text(json.dumps(RESULTS_FIXTURE))
    fw = ic.DraftFirewall.from_draft(DRAFT)
    fw.save(est / ".firewall_fingerprint.json")
    tab = ws / "03_entrega" / "tabelas"
    (tab / "tbl_caracteristicas.rds").write_bytes(b"RDS-placeholder")
    (tab / "tbl_caracteristicas.json").write_text(json.dumps({"label": "tbl-caracteristicas", "caption": "Características da amostra"}))
    (tab / "tbl_caracteristicas.md").write_text("| Variável | Valor |\n|---|---|\n| Idade | 72 |\n")
    fig = ws / "03_entrega" / "figuras"
    write_test_png(fig / "fig_efeito.png", 1800, 1200, 300)
    (fig / "fig_efeito.json").write_text(json.dumps({"label": "fig-efeito", "caption": "OR por PM2.5"}))
    st = StateManager(est / "sciforge_state.sqlite")
    for p in ("phase1", "phase2", "phase3", "phase4"):
        st.set_phase(p, "done")


def test_phases5_and_6_end_to_end_with_fake_llm(workspace):
    from sciforge.agents.phase5_storyboard import Phase5Storyboard
    from sciforge.agents.phase6_editorial import Phase6Editorial
    _seed_downstream_artifacts(workspace)
    provider = FakeProvider()
    ctx = _ctx(workspace, provider)
    asyncio.run(Phase5Storyboard(ctx).execute())
    assert json.loads((workspace / "01_estado" / "05_storyboard.json").read_text())["fio_condutor"]
    out = asyncio.run(Phase6Editorial(ctx).execute())
    entrega = workspace / "03_entrega"
    qmd = (entrega / "artigo_final.qmd").read_text()
    assert qmd.index("# Introdução") < qmd.index("# Métodos") < qmd.index("# Resultados") < qmd.index("# Discussão")
    assert 'readRDS("tabelas/tbl_caracteristicas.rds")' in qmd and "{#fig-efeito" in qmd
    assert "csl: estilo_alvo.csl" in qmd and (entrega / "estilo_alvo.csl").exists()
    assert parse_bib_keys((entrega / "referencias.bib").read_text()) == {"di2017air", "cohen2017estimates"}
    assert (entrega / "material_suplementar.qmd").exists()
    checklist = (entrega / "checklist_preenchido.md").read_text()
    assert "| 13a |" in checklist and "Resultados §1" in checklist
    assert "verificar (local inválido)" in checklist  # local inexistente apontado pelo LLM é rejeitado
    assert (entrega / "cover_letter.md").read_text().startswith("Prezado")
    report = (entrega / "relatorio_auditoria_final.md").read_text()
    assert "numeros_sem_correspondencia**: 0" in report
    assert out["erros_auditoria"] == 0, report
    # retomada granular: nova execução não chama o LLM para seções já concluídas
    n_calls = len(provider.calls)
    ctx.state.set_phase("phase6", "pending")
    asyncio.run(Phase6Editorial(_ctx(workspace, provider)).execute())
    new_calls = provider.calls[n_calls:]
    assert not any("Redator" in s for s, _ in new_calls)
    if shutil.which("pandoc"):
        assert (entrega / "artigo_final_previa.docx").exists() or (entrega / "artigo_final.docx").exists()


def test_orchestrator_pauses_at_human_gates(workspace):
    from sciforge.orchestrator import run_pipeline
    _seed_downstream_artifacts(workspace)
    st = StateManager(workspace / "01_estado" / "sciforge_state.sqlite")
    st.set_phase("phase4", "pending")
    ctx = _ctx(workspace, FakeProvider())
    # Fases 1-3 concluídas, Portão #1 pendente, terminal não interativo → para no gate1
    assert asyncio.run(run_pipeline(workspace, ctx=ctx)) == "gate1"
    assert st.phase_status("phase4") == "pending"
    st.set_gate("gate1", "approved")
    st.set_phase("phase4", "done")
    # Portão #2 pendente: Fase 5 não pode começar
    assert asyncio.run(run_pipeline(workspace, ctx=ctx)) == "gate2"
    assert st.phase_status("phase5") == "pending"
    # --auto-approve percorre o restante até a entrega final
    assert asyncio.run(run_pipeline(workspace, auto_approve=True, ctx=ctx)) == "phase6"
    assert st.gate_status("gate3") == "approved" and st.phase_status("phase6") == "done"


PHASE4_SCRIPTS = {
    "00_setup.R": 'source(Sys.getenv("SCIFORGE_R_HELPERS"))\nP <- sf_paths()\nset.seed(1)\n'
                  'dir.create(P$interm, showWarnings = FALSE, recursive = TRUE)\nwriteLines("{}", P$resultados)\n',
    "01_prep_dados.R": 'source(Sys.getenv("SCIFORGE_R_HELPERS"))\nP <- sf_paths()\n'
                       'dados <- read.csv(file.path(P$dados, "dados_sinteticos.csv"), stringsAsFactors = FALSE)\n'
                       'dados <- sf_flow_start(dados)\n'
                       'dados <- sf_flow_exclude(dados, !is.na(dados$pm25_media_7d), "PM2.5 ausente", "exposição não estimada")\n'
                       'dados <- sf_flow_exclude(dados, !is.na(dados$tabagismo), "tabagismo ausente", "confundidor essencial")\n'
                       'sf_flow_write(dados)\nsf_missing_report(dados, c("escolaridade"), "categoria ignorado", "casos completos")\n'
                       'dados$pm25_10 <- dados$pm25_media_7d / 10\nsaveRDS(dados, file.path(P$interm, "analitica.rds"))\n',
    "02_modelos_principais.R": 'source(Sys.getenv("SCIFORGE_R_HELPERS"))\nP <- sf_paths()\n'
                               'dados <- readRDS(file.path(P$interm, "analitica.rds"))\n'
                               'fit_mod <- glm(internacao_respiratoria ~ pm25_10 + idade + sexo + tabagismo, data = dados, family = binomial)\n'
                               'sf_results_set("diagnosticos_modelos", list(sf_diag_glm(fit_mod, "logistico_principal")))\n'
                               'sf_results_set("resultados_principais", sf_tidy_model(fit_mod, "logistico_principal", termos = "pm25_10"))\n'
                               'saveRDS(fit_mod, file.path(P$interm, "modelos.rds"))\n',
    "03_sensibilidade.R": 'source(Sys.getenv("SCIFORGE_R_HELPERS"))\nP <- sf_paths()\n'
                          'dados <- readRDS(file.path(P$interm, "analitica.rds"))\n'
                          'fit_sens <- glm(internacao_respiratoria ~ pm25_10 + idade + sexo + tabagismo + temperatura_media_7d, data = dados, family = binomial)\n'
                          'sf_results_append("diagnosticos_modelos", list(sf_diag_glm(fit_sens, "ajuste_temperatura")))\n'
                          'sf_results_set("resultados_sensibilidade", sf_tidy_model(fit_sens, "ajuste_temperatura", termos = "pm25_10"))\n',
    "04_tabelas_figuras.R": 'suppressPackageStartupMessages(library(ggplot2))\nsource(Sys.getenv("SCIFORGE_R_HELPERS"))\nP <- sf_paths()\n'
                            'dados <- readRDS(file.path(P$interm, "analitica.rds"))\n'
                            'tab_tbl <- aggregate(pm25_media_7d ~ sexo, data = dados, FUN = mean)\n'
                            'sf_save_table(tab_tbl, "tbl-caracteristicas", caption = "Características")\n'
                            'p_fig <- ggplot(dados, aes(pm25_media_7d)) + geom_histogram(bins = 20, fill = sf_palette(1)) + sf_theme()\n'
                            'sf_save_figure(p_fig, "fig-exposicao", caption = "Distribuição do PM2.5")\n'
                            'sf_results_set("tabelas", list("tbl-caracteristicas"))\nsf_results_set("figuras", list("fig-exposicao"))\n',
}


class FakeR(FakeProvider):
    def _answer(self, system, user):
        import re as _re
        if "Bioestatístico" in system:
            if "RESULTADO DA EXECUÇÃO" in user:  # auto-correção
                name = _re.search(r"SCRIPT (\S+\.R)", user).group(1)
                return f"```r\n{PHASE4_SCRIPTS[name]}```"
            name = _re.search(r"ESCREVA AGORA: 02_analise/(\S+\.R)", user).group(1)
            code = PHASE4_SCRIPTS[name]
            if name == "02_modelos_principais.R":
                code = code.replace("family = binomial", "family = binomiall")  # bug proposital
            return f"```r\n{code}```"
        if "QA Visual" in system:
            return json.dumps({"aprovado": True, "problemas": [], "instrucoes_correcao": []})
        return super()._answer(system, user)


@requires_r
def test_phase4_pipeline_with_autofix_and_contract(workspace):
    from sciforge.agents.phase4_analyst import Phase4Analyst
    _seed_downstream_artifacts(workspace)
    for sub in ("tabelas", "figuras"):
        for f in (workspace / "03_entrega" / sub).glob("*"):
            f.unlink()
    (workspace / "01_estado" / "01_inventario_semente.json").write_text(json.dumps({"ideia_nucleo": "PM2.5 e internação."}))
    provider = FakeR()
    settings = load_settings(workspace, fake_settings({"analysis": {"use_renv": False}}))
    client = LLMClient(settings, providers={"fake": provider})
    ctx = PhaseContext.create(workspace, settings_extra=fake_settings({"analysis": {"use_renv": False}}),
                              llm=client, log=lambda m: None)
    out = asyncio.run(Phase4Analyst(ctx).execute())
    assert out["tabelas"] == ["tbl-caracteristicas"] and out["figuras_reprovadas_qa"] == []
    assert ctx.state.attempts("r_script", "02_modelos_principais.R") == 1  # uma auto-correção
    res, problems = validate_results(workspace / "01_estado" / "04_resultados_verificados.json", 0.2)
    assert problems == [] and len(res.diagnosticos_modelos) == 2 and res.resultados_sensibilidade
    report = (workspace / "01_estado" / "04_relatorio_analise.md").read_text()
    assert "N analítico" in report and "aprovada" in report
