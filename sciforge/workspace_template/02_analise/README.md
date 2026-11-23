# 02_analise — projeto R reprodutível

Gerado pela Fase 4 do SciForge:

- `renv.lock`, `.Rprofile`, `renv/` — ambiente de pacotes congelado (`renv::restore()` reproduz).
- `00_setup.R` … `04_tabelas_figuras.R` — pipeline numerado; rode em ordem com `Rscript`.
- `intermediarios/` — bases analíticas e objetos de modelo (contêm dados individuais: não versionar).
- `sessionInfo.txt` — versões exatas do R e pacotes.

Para reexecutar manualmente a partir da raiz do projeto:

```bash
cd 02_analise
export SCIFORGE_WORKSPACE="$(cd .. && pwd)"
export SCIFORGE_R_HELPERS=/caminho/para/sciforge/scripts_r/export_table_helper.R
for f in 0*_*.R; do Rscript "$f"; done
```
