# Receitas Goose do SciForge

Uma receita por fase. Rode cada uma em uma **sessão nova** do Goose, na ordem:

| Receita | Fase | Portão humano |
|---|---|---|
| `01_intake.yaml` | Desconstrução da semente + Raio-X dos dados | — |
| `02_research_swarm.yaml` | Enxame de literatura + Evidence Ledger | — |
| `03_architect.yaml` | Protocolo + Revisor #2 | #1 |
| `04_analyst.yaml` | Análise em R + diagnósticos + QA visual | #2 |
| `05_storyboard.yaml` | Storyboard narrativo | — |
| `06_editorial.yaml` | Redação, auditorias, renderização, kit | #3 |

```powershell
goose run --recipe goose_recipes\01_intake.yaml --params project_dir=C:\pesquisa\meu_estudo
```

Isolamento do firewall anti-retalhos: as receitas carregam apenas a extensão `sciforge` (sem acesso
genérico a arquivos), cujas ferramentas nunca devolvem `00_semente/rascunho.*` nem os gráficos
preliminares. A Fase 1 lê o rascunho dentro do processo do SciForge; o agente vê só o inventário de
átomos conceituais. Os portões só são aprovados com a frase explícita do pesquisador.

As receitas chamam `sciforge-mcp` pelo nome: coloque `.venv\Scripts` no `PATH` ou troque `cmd` pelo
caminho completo do executável.
