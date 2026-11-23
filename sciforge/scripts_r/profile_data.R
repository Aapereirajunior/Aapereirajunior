#!/usr/bin/env Rscript
# SciForge — Raio-X agregado de datasets (.parquet, .csv, .tsv, .rds, .fst)
#
# Uso: Rscript profile_data.R <arquivo_entrada> <arquivo_saida_json> [min_celula=5]
#
# Garantias LGPD:
#   * Somente leitura: o arquivo de entrada nunca é modificado.
#   * Nenhum registro individual é impresso ou gravado; apenas agregados.
#   * Categorias com contagem < min_celula são suprimidas (n = NULL, suprimido = TRUE).
#   * Colunas com cara de identificador (CPF, CNS, nome, telefone, e-mail, endereço ou texto quase
#     único) têm níveis omitidos.

suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Pacote 'jsonlite' é obrigatório.")
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) stop("Uso: Rscript profile_data.R <entrada> <saida.json> [min_celula]")
in_path  <- normalizePath(args[[1]], winslash = "/", mustWork = TRUE)
out_path <- args[[2]]
min_cell <- if (length(args) >= 3) as.integer(args[[3]]) else 5L
max_levels <- 15L

ext <- tolower(tools::file_ext(in_path))
avisos <- character(0)
engine <- NA_character_

has <- function(pkg) requireNamespace(pkg, quietly = TRUE)

read_any <- function(path, ext) {
  if (ext %in% c("csv", "tsv")) {
    if (has("data.table")) {
      engine <<- "data.table"
      return(as.data.frame(data.table::fread(path, na.strings = c("", "NA", "NULL", "NaN"),
                                             showProgress = FALSE, encoding = "UTF-8")))
    }
    engine <<- "utils"
    sep <- if (ext == "tsv") "\t" else ","
    return(utils::read.csv(path, sep = sep, na.strings = c("", "NA", "NULL"), stringsAsFactors = FALSE,
                           fileEncoding = "UTF-8"))
  }
  if (ext == "parquet") {
    if (has("arrow")) {
      engine <<- "arrow"
      return(as.data.frame(arrow::read_parquet(path)))
    }
    if (has("duckdb") && has("DBI")) {
      engine <<- "duckdb"
      con <- DBI::dbConnect(duckdb::duckdb(), read_only = FALSE)
      on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
      q <- sprintf("SELECT * FROM read_parquet('%s')", gsub("'", "''", path))
      return(DBI::dbGetQuery(con, q))
    }
    stop("Leitura de .parquet requer o pacote 'arrow' ou 'duckdb'.")
  }
  if (ext == "rds") {
    engine <<- "base::readRDS"
    obj <- readRDS(path)
    if (!is.data.frame(obj)) stop("O .rds não contém um data.frame.")
    return(as.data.frame(obj))
  }
  if (ext == "fst") {
    if (!has("fst")) stop("Leitura de .fst requer o pacote 'fst'.")
    engine <<- "fst"
    return(as.data.frame(fst::read_fst(path)))
  }
  stop(sprintf("Formato não suportado: .%s", ext))
}

id_name_pattern <- "(^|_)(id|cpf|cns|cartao_sus|rg|nome|name|telefone|fone|celular|phone|email|e_mail|endereco|logradouro|address|cep|nis|pis)($|_)"

num_or_null <- function(x) if (length(x) == 0 || is.na(x) || !is.finite(x)) NULL else unname(signif(x, 6))

looks_like_date_string <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) == 0) return(FALSE)
  s <- utils::head(x, 200)
  mean(grepl("^\\d{4}-\\d{2}-\\d{2}", s) | grepl("^\\d{2}/\\d{2}/\\d{4}$", s)) > 0.9
}

parse_dates <- function(x) {
  x <- as.character(x)
  out <- as.Date(x, format = "%Y-%m-%d")
  alt <- is.na(out) & !is.na(x)
  if (any(alt)) out[alt] <- as.Date(x[alt], format = "%d/%m/%Y")
  out
}

profile_column <- function(name, x, n) {
  miss <- sum(is.na(x) | (is.character(x) & trimws(as.character(x)) == ""))
  res <- list(
    nome = name,
    tipo = class(x)[1],
    n_missing = as.integer(miss),
    pct_missing = round(100 * miss / max(n, 1), 2),
    n_unicos = as.integer(length(unique(x[!is.na(x)]))),
    possivel_identificador = FALSE
  )
  non_na <- x[!is.na(x)]
  uniq_ratio <- if (length(non_na) > 0) length(unique(non_na)) / length(non_na) else 0
  name_hit <- grepl(id_name_pattern, tolower(name), perl = TRUE)

  if (inherits(x, c("Date", "POSIXt")) || (is.character(x) && looks_like_date_string(x))) {
    d <- if (inherits(x, c("Date", "POSIXt"))) as.Date(x) else parse_dates(x)
    res$tipo <- "date"
    if (any(!is.na(d))) {
      res$data_min <- format(min(d, na.rm = TRUE))
      res$data_max <- format(max(d, na.rm = TRUE))
    }
    if (grepl("nasc|birth", tolower(name))) {
      res$possivel_identificador <- TRUE
      res$data_min <- NULL
      res$data_max <- NULL
      avisos <<- c(avisos, sprintf("Coluna '%s' parece data de nascimento: faixa omitida.", name))
    }
    return(res)
  }

  if (is.logical(x)) x <- as.integer(x)

  if (is.numeric(x)) {
    digits_like_id <- FALSE
    if (length(non_na) > 0 && all(non_na == round(non_na))) {
      nch <- nchar(format(utils::head(non_na, 500), scientific = FALSE, trim = TRUE))
      digits_like_id <- mean(nch %in% c(11, 15)) > 0.9 && uniq_ratio > 0.9
    }
    if (name_hit || digits_like_id) {
      res$possivel_identificador <- TRUE
      res$tipo <- "identificador"
      return(res)
    }
    if (res$n_unicos <= 10 && length(non_na) > 0 && all(non_na == round(non_na))) {
      res$tipo <- "numeric_categorico"
    } else {
      q <- stats::quantile(non_na, c(.25, .5, .75), names = FALSE, na.rm = TRUE)
      res$media   <- num_or_null(mean(non_na))
      res$dp      <- num_or_null(stats::sd(non_na))
      res$minimo  <- num_or_null(min(non_na))
      res$p25     <- num_or_null(q[1])
      res$mediana <- num_or_null(q[2])
      res$p75     <- num_or_null(q[3])
      res$maximo  <- num_or_null(max(non_na))
      return(res)
    }
  }

  # Categórico/texto
  xc <- as.character(x)
  if (name_hit || (is.character(x) && uniq_ratio > 0.9 && res$n_unicos > 50)) {
    res$possivel_identificador <- TRUE
    res$tipo <- "identificador"
    avisos <<- c(avisos, sprintf("Coluna '%s' tratada como possível identificador (níveis omitidos).", name))
    return(res)
  }
  tab <- sort(table(xc[!is.na(xc) & trimws(xc) != ""]), decreasing = TRUE)
  tab <- utils::head(tab, max_levels)
  res$niveis <- lapply(names(tab), function(lv) {
    cnt <- as.integer(tab[[lv]])
    if (cnt < min_cell) list(nivel = lv, n = NULL, suprimido = TRUE)
    else list(nivel = lv, n = cnt, suprimido = FALSE)
  })
  if (is.character(x) || is.factor(x)) res$tipo <- "categorico"
  res
}

df <- read_any(in_path, ext)
n <- nrow(df)
cols <- lapply(names(df), function(nm) profile_column(nm, df[[nm]], n))

out <- list(
  arquivo = basename(in_path),
  formato = ext,
  engine = engine,
  n_linhas = as.integer(n),
  n_colunas = as.integer(ncol(df)),
  colunas = cols,
  avisos = as.list(unique(avisos))
)

dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
jsonlite::write_json(out, out_path, auto_unbox = TRUE, null = "null", na = "null", pretty = TRUE, digits = NA)
cat(sprintf("Perfil gravado: %s (%d linhas, %d colunas)\n", basename(out_path), n, ncol(df)))
