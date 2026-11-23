#!/usr/bin/env Rscript
# SciForge — inicializa um projeto R reprodutível com renv no diretório de análise.
#
# Uso: Rscript init_renv.R <diretorio_analise> [pacote1,pacote2,...]
#
# Comportamento:
#   * Cria .Rprofile/renv.lock via renv::init(bare = TRUE) se ainda não existir.
#   * Instala pacotes ausentes (silenciosamente ignora os que falharem, listando-os ao final).
#   * Executa renv::snapshot() registrando as versões exatas usadas.
#   * Grava renv_status.json com pacotes instalados/faltantes para o orquestrador.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1) stop("Uso: Rscript init_renv.R <diretorio_analise> [pacotes]")
proj <- normalizePath(args[[1]], winslash = "/", mustWork = FALSE)
dir.create(proj, recursive = TRUE, showWarnings = FALSE)
pkgs <- if (length(args) >= 2 && nzchar(args[[2]])) strsplit(args[[2]], ",")[[1]] else
  c("tidyverse", "gtsummary", "gt", "flextable", "ggplot2", "jsonlite", "broom")
pkgs <- unique(trimws(pkgs))

status <- list(diretorio = proj, renv = FALSE, instalados = list(), faltantes = list(), mensagens = list())
msg <- function(...) { m <- sprintf(...); cat(m, "\n"); status$mensagens[[length(status$mensagens) + 1]] <<- m }

if (!requireNamespace("renv", quietly = TRUE)) {
  msg("renv não instalado; tentando instalar do CRAN.")
  try(utils::install.packages("renv", repos = "https://cloud.r-project.org", quiet = TRUE), silent = TRUE)
}

if (requireNamespace("renv", quietly = TRUE)) {
  setwd(proj)
  if (!file.exists(file.path(proj, "renv.lock"))) {
    ok <- tryCatch({
      renv::init(project = proj, bare = TRUE, restart = FALSE, settings = list(snapshot.type = "all"))
      TRUE
    }, error = function(e) { msg("renv::init falhou: %s", conditionMessage(e)); FALSE })
    status$renv <- ok
  } else {
    status$renv <- TRUE
    msg("renv.lock já existe; reutilizando.")
  }
  if (isTRUE(status$renv)) {
    # Carrega a biblioteca do projeto, mas permite usar pacotes do sistema como cache.
    try(renv::load(proj, quiet = TRUE), silent = TRUE)
    lib_sys <- .Library.site
    for (p in pkgs) {
      present <- requireNamespace(p, quietly = TRUE, lib.loc = c(.libPaths(), lib_sys))
      if (!present) {
        try(renv::install(p, prompt = FALSE), silent = TRUE)
        present <- requireNamespace(p, quietly = TRUE)
      }
      if (present) status$instalados[[length(status$instalados) + 1]] <- p
      else status$faltantes[[length(status$faltantes) + 1]] <- p
    }
    tryCatch(renv::snapshot(project = proj, prompt = FALSE, force = TRUE),
             error = function(e) msg("renv::snapshot falhou: %s", conditionMessage(e)))
  }
} else {
  msg("renv indisponível; seguindo sem isolamento de pacotes (registre sessionInfo manualmente).")
  for (p in pkgs) {
    if (requireNamespace(p, quietly = TRUE)) status$instalados[[length(status$instalados) + 1]] <- p
    else status$faltantes[[length(status$faltantes) + 1]] <- p
  }
}

con <- file(file.path(proj, "sessionInfo.txt"), open = "w", encoding = "UTF-8")
writeLines(capture.output(sessionInfo()), con)
close(con)
if (requireNamespace("jsonlite", quietly = TRUE)) {
  jsonlite::write_json(status, file.path(proj, "renv_status.json"), auto_unbox = TRUE, pretty = TRUE)
}
cat(sprintf("Pacotes disponíveis: %d | faltantes: %d\n", length(status$instalados), length(status$faltantes)))
