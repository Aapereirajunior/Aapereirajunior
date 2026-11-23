# Estilos CSL

- `vancouver.csl` — Vancouver/NLM numérico (cópia de `elsevier-vancouver.csl` do repositório oficial).
- `abnt.csl` — ABNT NBR 6023 (cópia de `associacao-brasileira-de-normas-tecnicas.csl`).

Qualquer outro estilo é baixado automaticamente pelo nome do arquivo no repositório oficial
<https://github.com/citation-style-language/styles> (o mesmo que alimenta o Zotero Style Repository):

```bash
sciforge csl the-lancet          # baixa e armazena em config/csl/the-lancet.csl
sciforge csl cadernos-de-saude-publica
```

No `regras_revista.txt`, a linha `Estilo: <nome>` define o CSL usado pelo projeto.
