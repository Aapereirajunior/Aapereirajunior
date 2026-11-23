# SciForge — helpers padronizados para os scripts da Fase 4.
#
# Carregue com: source(Sys.getenv("SCIFORGE_R_HELPERS"))
#
# Funções principais:
#   sf_paths()                     caminhos do workspace (via variáveis de ambiente do sandbox)
#   sf_theme(), sf_palette()       tema limpo e paleta Okabe-Ito (acessível a daltônicos)
#   sf_save_table(obj, nome)       grava tabela gtsummary/gt/flextable/data.frame como .rds + .html + .md
#   sf_save_figure(plot, nome)     grava ggplot como PNG 300 DPI + PDF + SVG (quando svglite disponível)
#   sf_results_init()/sf_results_set()/sf_results_write()  contrato 04_resultados_verificados.json
#   sf_flow_start()/sf_flow_exclude()  fluxo amostral STROBE/RECORD sem perda silenciosa
#   sf_tidy_model()/sf_diag_glm()  extração padronizada de estimativas e diagnósticos
#   sf_safe_summary(df)            resumo agregado seguro (nunca imprime linhas)

suppressPackageStartupMessages({
  stopifnot(requireNamespace("jsonlite", quietly = TRUE))
})

sf_paths <- function() {
  root <- Sys.getenv("SCIFORGE_WORKSPACE", unset = normalizePath(file.path(getwd(), ".."), winslash = "/", mustWork = FALSE))
  list(
    root      = root,
    dados     = file.path(root, "00_semente", "dados"),
    estado    = file.path(root, "01_estado"),
    analise   = file.path(root, "02_analise"),
    interm    = file.path(root, "02_analise", "intermediarios"),
    tabelas   = file.path(root, "03_entrega", "tabelas"),
    figuras   = file.path(root, "03_entrega", "figuras"),
    resultados = file.path(root, "01_estado", "04_resultados_verificados.json")
  )
}

.sf_dir <- function(p) { dir.create(p, recursive = TRUE, showWarnings = FALSE); p }

# Escrita de texto sempre em UTF-8 (no Windows, writeLines() sem conexão usa a codificação nativa em R < 4.2).
.sf_write_utf8 <- function(lines, path) {
  con <- file(path, open = "w", encoding = "UTF-8")
  on.exit(close(con), add = TRUE)
  writeLines(enc2utf8(as.character(lines)), con, useBytes = TRUE)
}

# --------------------------------------------------------------------------------- estética
sf_palette <- function(n = 8) {
  okabe <- c("#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#000000")
  if (n <= length(okabe)) okabe[seq_len(n)]
  else if (requireNamespace("viridisLite", quietly = TRUE)) viridisLite::viridis(n)
  else grDevices::hcl.colors(n, "viridis")
}

sf_theme <- function(base_size = 12) {
  ggplot2::theme_minimal(base_size = base_size) +
    ggplot2::theme(
      plot.title = ggplot2::element_text(face = "bold", size = base_size + 1),
      axis.title = ggplot2::element_text(size = base_size),
      axis.text = ggplot2::element_text(size = base_size - 1, colour = "grey20"),
      legend.position = "bottom",
      legend.text = ggplot2::element_text(size = base_size - 1),
      panel.grid.minor = ggplot2::element_blank(),
      plot.margin = ggplot2::margin(10, 14, 10, 10)
    )
}

# --------------------------------------------------------------------------------- tabelas
sf_save_table <- function(obj, nome, caption = NULL) {
  stopifnot(grepl("^tbl[-_][a-z0-9_-]+$", nome))
  dir_t <- .sf_dir(sf_paths()$tabelas)
  base <- file.path(dir_t, gsub("-", "_", nome))
  saveRDS(obj, paste0(base, ".rds"))

  md <- NULL
  html_ok <- FALSE
  if (inherits(obj, "gtsummary")) {
    md <- tryCatch(capture.output(print(gtsummary::as_kable(obj, format = "pipe"))), error = function(e) NULL)
    if (requireNamespace("gt", quietly = TRUE)) {
      html_ok <- tryCatch({ gt::gtsave(gtsummary::as_gt(obj), paste0(base, ".html")); TRUE },
                          error = function(e) FALSE)
    }
  } else if (inherits(obj, "gt_tbl")) {
    html_ok <- tryCatch({ gt::gtsave(obj, paste0(base, ".html")); TRUE }, error = function(e) FALSE)
    md <- tryCatch(capture.output(print(knitr::kable(gt::extract_body(obj), format = "pipe"))),
                   error = function(e) NULL)
  } else if (inherits(obj, "flextable")) {
    html_ok <- tryCatch({ flextable::save_as_html(obj, path = paste0(base, ".html")); TRUE },
                        error = function(e) FALSE)
  } else if (is.data.frame(obj)) {
    md <- .sf_df_to_md(obj)
  }
  if (!is.null(md)) .sf_write_utf8(md, paste0(base, ".md"))
  meta <- list(label = gsub("_", "-", nome), arquivo_rds = basename(paste0(base, ".rds")),
               classe = class(obj)[1], caption = caption, html = html_ok, md = !is.null(md))
  jsonlite::write_json(meta, paste0(base, ".json"), auto_unbox = TRUE, pretty = TRUE, null = "null")
  message(sprintf("[sciforge] tabela salva: %s", basename(base)))
  invisible(paste0(base, ".rds"))
}

.sf_df_to_md <- function(df) {
  df <- as.data.frame(df)
  fmt <- function(v) if (is.numeric(v)) formatC(v, digits = 3, format = "fg", flag = "#") else as.character(v)
  cells <- lapply(df, fmt)
  header <- paste0("| ", paste(names(df), collapse = " | "), " |")
  sep <- paste0("|", paste(rep("---", ncol(df)), collapse = "|"), "|")
  rows <- vapply(seq_len(nrow(df)), function(i) paste0("| ", paste(vapply(cells, `[`, "", i), collapse = " | "), " |"), "")
  c(header, sep, rows)
}

# --------------------------------------------------------------------------------- figuras
sf_save_figure <- function(plot, nome, width = 7, height = 5, dpi = 300, caption = NULL) {
  stopifnot(grepl("^fig[-_][a-z0-9_-]+$", nome))
  dir_f <- .sf_dir(sf_paths()$figuras)
  base <- file.path(dir_f, gsub("-", "_", nome))
  ggplot2::ggsave(paste0(base, ".png"), plot, width = width, height = height, dpi = dpi, bg = "white")
  ggplot2::ggsave(paste0(base, ".pdf"), plot, width = width, height = height, device = .sf_pdf_device())
  if (requireNamespace("svglite", quietly = TRUE)) {
    ggplot2::ggsave(paste0(base, ".svg"), plot, width = width, height = height, device = svglite::svglite)
  }
  meta <- list(label = gsub("_", "-", nome), png = basename(paste0(base, ".png")), dpi = dpi,
               width_in = width, height_in = height, caption = caption)
  jsonlite::write_json(meta, paste0(base, ".json"), auto_unbox = TRUE, pretty = TRUE, null = "null")
  message(sprintf("[sciforge] figura salva: %s", basename(base)))
  invisible(paste0(base, ".png"))
}

# Dispositivo PDF: cairo_pdf (melhor suporte a UTF-8/fontes) quando disponível; senão o pdf() padrão.
.sf_pdf_device <- function() {
  if (isTRUE(capabilities("cairo"))) grDevices::cairo_pdf else "pdf"
}

# Coalescência de NULL sem sobrescrever o `%||%` nativo do R >= 4.4.
.sf_or <- function(a, b) if (is.null(a)) b else a

# --------------------------------------------------------------------------------- contrato de resultados
sf_results_read <- function() {
  p <- sf_paths()$resultados
  if (file.exists(p)) jsonlite::read_json(p, simplifyVector = FALSE) else list()
}

sf_results_set <- function(key, value) {
  res <- sf_results_read()
  res[[key]] <- value
  .sf_dir(dirname(sf_paths()$resultados))
  jsonlite::write_json(res, sf_paths()$resultados, auto_unbox = TRUE, pretty = TRUE, digits = NA,
                       null = "null", na = "null")
  invisible(res)
}

sf_results_append <- function(key, items) {
  res <- sf_results_read()
  res[[key]] <- c(.sf_or(res[[key]], list()), items)
  jsonlite::write_json(res, sf_paths()$resultados, auto_unbox = TRUE, pretty = TRUE, digits = NA,
                       null = "null", na = "null")
  invisible(res)
}

# Fluxo amostral: toda exclusão passa por sf_flow_exclude, impedindo perdas silenciosas.
sf_flow_start <- function(df) {
  .sf_flow <<- list(n_inicial = nrow(df), exclusoes = list(), n_final = nrow(df))
  df
}

sf_flow_exclude <- function(df, keep, criterio, justificativa = NULL) {
  keep[is.na(keep)] <- FALSE
  removed <- sum(!keep)
  .sf_flow$exclusoes[[length(.sf_flow$exclusoes) + 1]] <<- list(criterio = criterio, n_removido = removed,
                                                                 justificativa = justificativa)
  .sf_flow$n_final <<- .sf_flow$n_final - removed
  df[keep, , drop = FALSE]
}

sf_flow_write <- function(df) {
  if (nrow(df) != .sf_flow$n_final) stop("Fluxo amostral divergente do N analítico: perda silenciosa.")
  sf_results_set("fluxo_amostral", .sf_flow)
}

sf_missing_report <- function(df, vars, metodo, metodo_geral = metodo) {
  items <- lapply(vars, function(v) list(variavel = v, pct_missing = round(100 * mean(is.na(df[[v]])), 2),
                                         metodo = metodo))
  sf_results_set("tratamento_missing", list(variaveis = items, metodo_geral = metodo_geral))
}

# --------------------------------------------------------------------------------- modelos
sf_tidy_model <- function(fit, modelo, termos = NULL, exponentiate = TRUE, medida = "OR") {
  stopifnot(requireNamespace("broom", quietly = TRUE))
  td <- broom::tidy(fit, conf.int = TRUE, exponentiate = exponentiate)
  if (!is.null(termos)) td <- td[td$term %in% termos, , drop = FALSE]
  td <- td[td$term != "(Intercept)", , drop = FALSE]
  n_obs <- tryCatch(stats::nobs(fit), error = function(e) NA_integer_)
  lapply(seq_len(nrow(td)), function(i) list(
    modelo = modelo, termo = td$term[i], medida = medida,
    estimativa = unname(td$estimate[i]), ic95_inf = unname(td$conf.low[i]),
    ic95_sup = unname(td$conf.high[i]), p_valor = unname(td$p.value[i]), n = as.integer(n_obs)))
}

sf_diag_glm <- function(fit, modelo) {
  fam <- tryCatch(stats::family(fit)$family, error = function(e) NA_character_)
  disp <- tryCatch(sum(stats::residuals(fit, type = "pearson")^2) / stats::df.residual(fit),
                   error = function(e) NA_real_)
  avisos <- character(0)
  if (!isTRUE(fit$converged)) avisos <- c(avisos, "Algoritmo IRLS não convergiu.")
  if (isTRUE(fit$boundary)) avisos <- c(avisos, "Ajuste na fronteira do espaço paramétrico.")
  out <- list(modelo = modelo, converged = isTRUE(fit$converged) && !isTRUE(fit$boundary), familia = fam,
              dispersao = disp, superdispersao = if (fam %in% c("poisson", "binomial")) disp > 1.5 else NULL,
              aic = tryCatch(stats::AIC(fit), error = function(e) NULL),
              bic = tryCatch(stats::BIC(fit), error = function(e) NULL),
              pressupostos = structure(list(), names = character(0)), avisos = as.list(avisos))
  if (identical(fam, "binomial")) {
    y <- fit$y; p <- stats::fitted(fit)
    if (length(unique(y)) == 2) {
      r <- rank(p); n1 <- sum(y == 1); n0 <- sum(y == 0)
      out$auc <- (sum(r[y == 1]) - n1 * (n1 + 1) / 2) / (n1 * n0)
      qs <- unique(stats::quantile(p, seq(0, 1, .1)))
      if (length(qs) < 3) {
        out$calibracao <- "Hosmer-Lemeshow não calculado: probabilidades previstas com menos de 3 grupos distintos"
      } else {
        grp <- cut(p, qs, include.lowest = TRUE)
        obs <- tapply(y, grp, sum); exp_ <- tapply(p, grp, sum); nn <- tapply(y, grp, length)
        keep <- !is.na(obs) & !is.na(exp_)
        obs <- obs[keep]; exp_ <- exp_[keep]; nn <- nn[keep]
        if (length(obs) <= 2) {
          out$calibracao <- "Hosmer-Lemeshow não calculado: 2 ou menos grupos não vazios"
        } else {
          hl <- sum((obs - exp_)^2 / (exp_ * (1 - exp_ / nn)), na.rm = TRUE)
          out$calibracao <- sprintf("Hosmer-Lemeshow chi2=%.2f, gl=%d, p=%.3f", hl, length(obs) - 2,
                                    stats::pchisq(hl, length(obs) - 2, lower.tail = FALSE))
        }
      }
    }
  }
  out
}

# --------------------------------------------------------------------------------- inspeção segura
sf_safe_summary <- function(df) {
  data.frame(variavel = names(df),
             classe = vapply(df, function(x) class(x)[1], ""),
             pct_missing = vapply(df, function(x) round(100 * mean(is.na(x)), 1), 0),
             n_unicos = vapply(df, function(x) length(unique(x)), 0L),
             row.names = NULL)
}
