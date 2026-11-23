# SciForge v2 — R/Quarto Edition

Sistema multi-agente que **reconstrói** um artigo epidemiológico a partir de uma semente (rascunho + dados
reais), em vez de revisar o rascunho por cima. A orquestração é em **Python** (assincronismo, validação,
agentes, MCP); a análise e a editoração são nativas em **R** (`tidyverse`, `gtsummary`, `ggplot2`, `renv`)
e **Quarto** (`.qmd` + Pandoc + CSL).

```
00_semente/ ──► Fase 1 Intake ──► Fase 2 Enxame ──► Fase 3 Arquiteto + Revisor #2 ──► [Portão 1]
  rascunho        (única que       4 agentes          protocolo PECO/PICO
  dados           lê o rascunho)   + Evidence Ledger
                       │
                       ▼ firewall anti-retalhos ativado
[Portão 2] ◄── Fase 4 Análise R (renv, sandbox LGPD, diagnósticos, QA visual)
     │
     ▼
Fase 5 Storyboard ──► Fase 6 Redação Quarto + auditorias + Humanizador + Revisor #2 ──► [Portão 3]
                                                                     .docx / .pdf / Kit de Submissão
```

---

## 1. Instalação

### 1.1 Python (≥ 3.10)

```bash
cd sciforge
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                                 # instala o comando `sciforge`
cp .env.example .env                                    # preencha as chaves
```

**Windows (PowerShell):**

```powershell
cd sciforge
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # se bloqueado: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"
Copy-Item .env.example .env
```

O SciForge não depende de comandos de shell: caminhos usam `pathlib`, toda leitura/escrita de arquivo e
todo subprocesso usam UTF-8, e `Rscript`/`quarto`/`pandoc` são localizados automaticamente
(`PATH` → `R_HOME` → registro do Windows → `C:\Program Files\R\R-*\bin`, escolhendo a versão mais
recente; Quarto em `Program Files\Quarto` ou `%LOCALAPPDATA%\Programs\Quarto`; Pandoc embutido no Quarto).
Para forçar um executável, defina `SCIFORGE_RSCRIPT`, `SCIFORGE_QUARTO` ou `SCIFORGE_PANDOC` no `.env`.

O modo editável (`-e`) é o recomendado: os prompts (`prompts/`), as configurações (`config/`) e os
utilitários R (`scripts_r/`) são lidos da pasta do repositório. Se instalar de outra forma, defina
`SCIFORGE_HOME=/caminho/para/sciforge`.

### 1.2 R (≥ 4.2) e pacotes

Instale o R pelo [CRAN](https://cran.r-project.org) e, no console R:

```r
install.packages(c("renv", "jsonlite", "data.table", "arrow", "fst", "duckdb", "DBI",
                   "tidyverse", "gtsummary", "gt", "flextable", "broom", "knitr", "rmarkdown",
                   "viridisLite", "svglite", "ggrepel",
                   # conforme o protocolo:
                   "dlnm", "gnm", "mgcv", "fixest", "survival", "mice"))
```

A Fase 4 cria um projeto `renv` isolado em `02_analise/` e registra versões exatas em `renv.lock`.

### 1.3 Quarto

Baixe em <https://quarto.org/docs/get-started/> e, para PDF:

```bash
quarto install tinytex
quarto check
```

Sem Quarto, o SciForge ainda gera uma **prévia `.docx` via Pandoc + citeproc** (tabelas a partir das
prévias `.md`), marcada como prévia no relatório.

### 1.4 Ollama (obrigatório para `privacy_mode: strict` ou `hybrid`)

```bash
# https://ollama.com/download
ollama pull qwen2.5-coder:7b       # papel "local": escreve e corrige os scripts R da Fase 4
ollama pull qwen2.5vl:3b           # papel "local_vision": QA visual das figuras na Fase 4
ollama serve                       # OLLAMA_HOST=http://localhost:11434
```

O padrão é leve, pensado para GPUs de **4–8 GB de VRAM** (modelos 7B/3B quantizados, contexto 8192).
Ajuste à sua placa pelo `.env`, sem editar o `settings.yaml`:

| VRAM | `SCIFORGE_OLLAMA_MODEL` | `SCIFORGE_OLLAMA_VISION_MODEL` | `SCIFORGE_OLLAMA_NUM_CTX` |
|---|---|---|---|
| 4–8 GB | `qwen2.5-coder:7b` | `qwen2.5vl:3b` | 8192 |
| 12 GB+ | `qwen2.5-coder:14b` | `qwen2.5vl:7b` | 16384 |
| 24 GB+ | `qwen2.5-coder:32b` | `qwen2.5vl:7b` | 32768 |

Modelos de 7B erram mais ao escrever R: espere usar mais das 4 tentativas de auto-correção da Fase 4.

### 1.5 Variáveis de ambiente (`.env`)

| Variável | Uso |
|---|---|
| `GEMINI_API_KEY` | Fases 1–2 e QA visual (padrão) |
| `ANTHROPIC_API_KEY` | Claude (Fases 3, 5 e 6 no padrão), via SDK oficial `anthropic` |
| `OPENROUTER_API_KEY` (`OPENROUTER_BASE_URL` opcional) | Revisor #2 no padrão (`openai/gpt-4.1`); qualquer modelo do OpenRouter |
| `SCIFORGE_OLLAMA_MODEL`, `SCIFORGE_OLLAMA_VISION_MODEL`, `SCIFORGE_OLLAMA_NUM_CTX` | modelos e contexto do Ollama (padrão leve para 4–8 GB) |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | provedor `openai` (qualquer endpoint OpenAI-compatível) |
| `OLLAMA_HOST` | modelos locais |
| `POLITE_EMAIL` | `User-Agent` com `mailto:` (polite pool OpenAlex/Crossref) |
| `SEMANTIC_SCHOLAR_API_KEY` | opcional; sem ela a Semantic Scholar costuma responder 429 e o enxame segue com as outras fontes |

---

## 2. Primeiro projeto

```bash
sciforge init meu_estudo
cp ~/rascunho.docx            meu_estudo/00_semente/rascunho.docx   # .md, .txt, .pdf ou .docx
cp ~/dados/*.parquet          meu_estudo/00_semente/dados/
cp ~/graficos/*.png           meu_estudo/00_semente/graficos_preliminares/   # opcional
$EDITOR                       meu_estudo/00_semente/regras_revista.txt
sciforge run meu_estudo
```

`regras_revista.txt` aceita linhas como `Revista:`, `Idioma:`, `Estilo:` (nome CSL), `Limite de palavras:`,
`Resumo: até N palavras`, `Estrutura do resumo:`, `Tabelas e figuras: até N`, `Referências: até N`.

### Comandos

| Comando | Função |
|---|---|
| `sciforge init <projeto>` | cria o workspace a partir de `workspace_template/` |
| `sciforge phase <projeto> phaseN` | executa uma única fase (exige fases e portões anteriores concluídos) |
| `sciforge models [projeto]` | mostra qual provedor/modelo atende cada papel em cada fase |
| `sciforge run <projeto> [--until phaseN] [--auto-approve]` | executa as fases pendentes, pausando nos portões |
| `sciforge resume <projeto>` | retoma do ponto exato (fase e seção) em que parou |
| `sciforge status <projeto>` | fases, portões, chamadas, tokens, custo e eventos |
| `sciforge approve <projeto> gate1\|gate2\|gate3` | aprova um portão sem terminal interativo |
| `sciforge reset <projeto> <fase>` | invalida a fase e as seguintes (ex.: depois de editar o protocolo) |
| `sciforge render <projeto> --formatos docx,pdf` | renderiza de novo com Quarto |
| `sciforge audit <projeto>` | roda as auditorias determinísticas no `.qmd` atual |
| `sciforge csl <estilo>` | baixa um estilo do repositório oficial `citation-style-language/styles` |

### Portões humanos

1. **Portão #1 (após a Fase 3)**: resumo do protocolo + críticas do Revisor #2. Você pode editar
   `01_estado/03_protocolo_proposto.md` antes de aprovar; a Fase 4 lê a versão editada.
2. **Portão #2 (após a Fase 4)**: `01_estado/04_relatorio_analise.md` com fluxo amostral, missing,
   diagnósticos, estimativas e QA das figuras; tabelas em `03_entrega/tabelas/`, figuras em `03_entrega/figuras/`.
3. **Portão #3 (após a Fase 6)**: entrega final e `relatorio_auditoria_final.md`.

Em terminal não interativo (CI, nohup), o pipeline para no portão e indica o comando `sciforge approve`.

### O que sai em `03_entrega/`

`artigo_final.qmd/.docx/.pdf`, `material_suplementar.qmd/.docx/.pdf`, `referencias.bib` (apenas o que foi
citado), `estilo_alvo.csl`, `tabelas/` (`.rds` + `.html`/`.md`), `figuras/` (PNG 300 DPI + PDF + SVG),
`checklist_preenchido.md`, `cover_letter.md`, `relatorio_auditoria_final.md`.

---

## 3. As 8 travas de engenharia (onde estão no código)

| Trava | Implementação |
|---|---|
| **1. Firewall anti-retalhos** | `integrity_checks.guarded_read` (só a `phase1` abre `rascunho.*` e `graficos_preliminares/`); `InventarioSemente` (Pydantic) rejeita parágrafos, mais de 3 frases em `ideia_nucleo` e chaves de seção; `inventory_copy_check` rejeita 12-gramas copiados e força reescrita; `DraftFirewall` grava **apenas hashes** de n-gramas e é instalado como `prompt_guard` do `LLMClient`, barrando qualquer prompt das Fases 2-6 com trechos do rascunho; `draft_independence_hashed` alerta se mais de 10% das frases finais forem recicladas, sem reabrir o rascunho. Campos `contexto_tacito_local` e `glossario_obrigatorio` preservam conhecimento de domínio. |
| **2. Evidence Ledger** | `phase2_research_swarm._build_ledger` ficha cada artigo (resumo ou texto completo OA do EuropePMC) em `02_evidence_ledger.json`; `integrity_checks.citation_audit` confere cada `[@citekey]` contra `.bib`, ledger, status de retratação e **suporte** (sobreposição lexical + juiz LLM). |
| **3. Contrato de diagnósticos** | `schemas.ResultadosVerificados` / `FluxoAmostral` (perda silenciosa → erro), helpers R `sf_flow_*`, `sf_diag_glm`, `sf_tidy_model`; `phase4_analyst.validate_results` bloqueia o Portão #2 em caso de não convergência, Poisson superdisperso ou perda > limite sem justificativa. |
| **4. Tabelas determinísticas** | `04_tabelas_figuras.R` + `sf_save_table` → `.rds`; `quarto_renderer.table_chunk` insere chunks que só fazem `readRDS()`; o redator usa marcadores `{{tbl-...}}` e cita `@tbl-...`. |
| **5. Sandbox e LGPD** | `r_sandbox.static_check` bloqueia `head/tail/View/glimpse/print(df)`, auto-print, `system`, downloads e escrita em `00_semente`; `sanitize_output` mascara CPF, CNS, datas, e-mails, telefones e números longos; `LLMClient.resolve` força Ollama nas Fases 1 e 4 (`hybrid`) ou em tudo (`strict`) e levanta `PrivacyViolation` se o papel local apontar para a nuvem; `profile_data.R` suprime células < 5 e omite identificadores. |
| **6. QA visual** | `sf_save_figure` (300 DPI PNG + PDF + SVG, `sf_theme`, Okabe-Ito/viridis); `visual_inspector.deterministic_checks` (DPI via chunk pHYs, largura, versão vetorial) + inspeção multimodal com `04b_figure_qa.md`; até 2 ciclos de regeneração por figura. |
| **7. Quarto + CSL + Kit** | `quarto_renderer.build_qmd` (YAML com `bibliography`, `csl`, `crossref`, `reference-doc`), `ensure_csl` (Vancouver/ABNT locais + download pelo nome), `render`/`render_project`, checklist com locais validados contra os parágrafos reais, carta ao editor e relatório de auditoria. |
| **8. Custo e checkpoints** | `budget_guard.BudgetGuard` (tokens/custo/chamadas por fase, teto global, 4 correções R, 2 ciclos de QA, 2 rodadas de revisão); `state_manager.StateManager` (SQLite: fases, etapas, seções, uso, tentativas, portões); cada seção da Fase 6 é um checkpoint. |

### Roteamento multi-modelo por fase

`config/settings.yaml → llm.phases` define provedor e modelo por fase e por papel. Ordem de resolução:
`phases.<fase>.<papel>` → `phases.<fase>."*"` → `roles.<papel>` → `roles.default`. Padrão entregue:

| Fase | Modelo principal | Observações |
|---|---|---|
| 1 Intake | Gemini 2.5 Pro (visão: Gemini 2.5 Flash) | em `hybrid`/`strict` vai para o Ollama |
| 2 Enxame | Gemini 2.5 Pro (fichamento: Flash) | |
| 3 Arquiteto | Claude Opus 5.5 | Revisor #2 via OpenRouter (`openai/gpt-4.1`), outra família de modelo |
| 4 Análise | Claude Opus 5.5 / QA visual Gemini (só em `cloud`) | em `hybrid`/`strict` tudo no Ollama |
| 5 Storyboard | Claude Opus 5.5 | |
| 6 Redação | Claude Opus 5.5 | juiz de citações/checklist: Gemini Flash; Revisor #2 via OpenRouter |

Provedores disponíveis (`kind`): `gemini`, `anthropic` (SDK oficial, streaming, `output_config.effort` e
fallback de recusa no servidor — `fallbacks: null` desativa), `openai`, `openrouter` (endpoint
`https://openrouter.ai/api/v1`, slugs como `openai/gpt-4.1`; confira os nomes em openrouter.ai/models) e
`ollama`. Exemplo de override por projeto (`<projeto>/01_estado/settings.override.yaml`):

```yaml
llm:
  phases:
    phase6:
      "*": { provider: openrouter, model: openai/gpt-4.1 }
```

`sciforge models` imprime a tabela efetiva. O custo de cada chamada é contabilizado por fase com os
preços de `llm.pricing` (modelos via OpenRouter usam o slug como chave).

### `privacy_mode`

| Modo | Fase 1 e Fase 4 | Demais fases |
|---|---|---|
| `strict` | Ollama | Ollama |
| `hybrid` (padrão) | Ollama | nuvem, apenas com dados agregados (inventário, `04_resultados_verificados.json`) |
| `cloud` | nuvem | nuvem (use apenas com dados públicos ou anonimizados) |

---

## 4. Servidor MCP (IDE e Goose)

Ferramentas utilitárias: `search_literature_swarm`, `profile_r_dataset`, `verify_doi_and_retraction`,
`audit_quarto_manuscript`, `render_quarto_submission`.

Operação do fluxo: `sciforge_init_project`, `sciforge_status`, `sciforge_run_phase`,
`sciforge_approve_gate` (exige a frase de aprovação do pesquisador), `sciforge_read_artifact` (lista
branca de artefatos; nunca devolve o rascunho) e `sciforge_update_protocol` (edições antes do Portão #1,
com checagem do firewall).

**STDIO limpo:** o stdout é exclusivo do JSON-RPC. Todo logging vai para stderr, cada ferramenta roda com
`sys.stdout` redirecionado para stderr e os subprocessos (R, Quarto, Pandoc) têm saída capturada. Um
teste sobe o servidor real e exige que toda linha do stdout seja JSON-RPC válido.

Bloco para Claude Desktop/Claude Code (`.mcp.json`), Cursor (`.cursor/mcp.json`) ou VS Code:

```json
{
  "mcpServers": {
    "sciforge": {
      "command": "sciforge-mcp",
      "args": [],
      "env": {
        "POLITE_EMAIL": "pesquisador@instituicao.br",
        "SCIFORGE_HOME": "/caminho/absoluto/para/sciforge"
      }
    }
  }
}
```

Se o executável não estiver no `PATH` da IDE, use `"command": "/caminho/.venv/bin/python"` e
`"args": ["-m", "sciforge.mcp_server.server"]`. O servidor funciona com o SDK `mcp` 1.x (`FastMCP`) e 2.x
(`MCPServer`).

### Goose (Windows)

1. Instale o SciForge como na seção 1.1 e descubra o caminho do executável:

   ```powershell
   (Get-Command sciforge-mcp).Source
   # ex.: C:\Users\voce\sciforge\.venv\Scripts\sciforge-mcp.exe
   ```

2. **Registro permanente:** adicione ao arquivo de configuração do Goose
   (`%APPDATA%\Block\goose\config\config.yaml`; confirme o local com `goose info -v`):

   ```yaml
   extensions:
     sciforge:
       enabled: true
       type: stdio
       name: sciforge
       cmd: C:\Users\voce\sciforge\.venv\Scripts\sciforge-mcp.exe
       args: []
       timeout: 7200
   ```

   Ou, para uma sessão avulsa:

   ```powershell
   goose session --with-extension "C:\Users\voce\sciforge\.venv\Scripts\sciforge-mcp.exe"
   ```

   (Confira os flags da sua versão com `goose session --help`; `goose configure` → *Add Extension* →
   *Command-line Extension* faz o mesmo registro de forma interativa.)

3. **Receitas por fase** (`goose_recipes/`): cada uma carrega só a extensão `sciforge`, sem extensões de
   leitura de arquivos, e deve rodar em uma sessão nova do Goose. Assim o rascunho nunca entra no contexto
   do agente. As receitas chamam `sciforge-mcp` pelo nome, então ponha o venv no `PATH` antes:

   ```powershell
   $env:Path = "$PWD\.venv\Scripts;$env:Path"
   goose run --recipe goose_recipes\01_intake.yaml --params project_dir=C:\pesquisa\meu_estudo
   goose run --recipe goose_recipes\02_research_swarm.yaml --params project_dir=C:\pesquisa\meu_estudo
   # ... 03_architect (Portão #1), 04_analyst (Portão #2), 05_storyboard, 06_editorial (Portão #3)
   ```

   Nos portões, o Goose mostra o resumo e só chama `sciforge_approve_gate` depois que você escrever que
   aprova.

---

## 5. Estrutura

```
goose_recipes/     6 receitas Goose (uma por fase)
config/            settings.yaml · csl/ (vancouver, abnt) · checklists/ (STROBE, RECORD, RECORD_PE, TRIPOD_AI)
scripts_r/         profile_data.R · init_renv.R · export_table_helper.R (helpers sf_*)
src/core/          config · platform_utils · schemas (Pydantic) · llm_client · budget_guard · state_manager
src/tools/         literature_swarm · data_profiler · r_sandbox · visual_inspector · integrity_checks · quarto_renderer
src/agents/        base · phase1_intake … phase6_editorial
src/mcp_server/    server.py
src/orchestrator.py  CLI Typer/Rich
prompts/           system prompts editáveis (01 … 06c)
workspace_template/  esqueleto copiado por `sciforge init`
tests/             fixtures sintéticas + test_pipeline_components.py + test_hardening.py
```

Os prompts em `prompts/*.md` são lidos a cada chamada: edite-os para ajustar estilo, rigor ou idioma
sem tocar no código.

---

## 6. Testes

```bash
pytest -q                                   # unidade + ponta a ponta com LLM falso (R necessário para alguns)
SCIFORGE_RUN_INTEGRATION=1 pytest -m integration   # consulta as APIs reais de literatura
```

`test_hardening.py` cobre: varredura sem `TODO`/`NotImplementedError`/`pass`/funções vazias; análise
estática garantindo que `phase2`…`phase6` não referenciam `rascunho.*` nem os gráficos preliminares (e um
teste que falha se as Fases 5–6 tentarem abrir esses arquivos em tempo de execução); toda E/S com
`encoding="utf-8"` e subprocessos só via `platform_utils.run_command`; resolução de `Rscript`/`quarto`
num layout de Windows simulado; sessão STDIO real do MCP; receitas Goose; roteamento por fase; provedores
Claude e OpenRouter.

As fixtures em `tests/fixtures/` são **sintéticas**: `dados_sinteticos.csv` (N = 500, com missings
propositais, uma categoria rara para testar a supressão de células e identificadores falsos),
`rascunho.md` e `regras_revista.txt`. Para regenerar o CSV: `python tests/fixtures/gerar_dataset_sintetico.py`.

O teste ponta a ponta usa um provedor LLM falso e determinístico para validar a mecânica (Fase 1 com R
real, Fases 5 e 6 completas: storyboard, redação encadeada, humanizador, auditorias, revisor, montagem do
`.qmd`, checklist, carta e retomada sem novas chamadas). A qualidade científica do texto depende dos
modelos reais configurados.

## 7. Limitações conhecidas

- O suporte de citações combina heurística lexical com juiz LLM; alegações sutis ainda exigem leitura humana no Portão #3.
- Os checklists em `config/checklists/` são paráfrases; confira o texto oficial antes da submissão.
- A Fase 4 depende da capacidade do modelo local de escrever R correto; o sandbox limita a 4 correções por script e então bloqueia com o log sanitizado para intervenção humana.
- `vancouver.csl` é uma cópia de `elsevier-vancouver.csl`, porque o repositório oficial não tem mais um `vancouver.csl` na raiz.
