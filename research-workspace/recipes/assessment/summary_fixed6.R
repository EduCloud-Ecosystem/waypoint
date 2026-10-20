# SPDX-License-Identifier: Apache-2.0
# Disabled assessment candidate. INPUT CONDITION EXPECTED_ROWS; "-" is stdin.
# Base R only. No sampling, execution authority, storage or cleanup claim.

B <- 1000000
WIDTH <- 5L
MAX_ROWS <- 1000000
MAX_BYTES <- 88000013
MAX_GROUPS <- 128L
CHUNK_BYTES <- 65536L
MAX_RECORD <- 88L
MAX_OUTPUT <- 61440L

fail <- function(kind = "summary_invalid") {
  stop(structure(list(message = "Summary refused.", call = NULL),
                 class = c(kind, "error", "condition")))
}

normal <- function(sign, digits) {
  if (length(digits) != WIDTH || any(!is.finite(digits)) ||
      any(digits != floor(digits)) || any(digits < 0 | digits >= B) ||
      !(sign %in% c(-1, 0, 1)) ||
      (sign == 0 && any(digits != 0))) fail("summary_internal")
  list(sign = if (all(digits == 0)) 0 else sign, digits = digits)
}

zero <- function() normal(0, rep(0, WIDTH))
MAX_VALUE <- normal(1, c(0, 0, 0, 1, 0))
MAX_SUM <- normal(1, c(0, 0, 0, 0, 1))

compare_magnitude <- function(a, b) {
  for (i in WIDTH:1L) {
    if (a[i] < b[i]) return(-1)
    if (a[i] > b[i]) return(1)
  }
  0
}

compare <- function(a, b) {
  if (a$sign != b$sign) return(sign(a$sign - b$sign))
  a$sign * compare_magnitude(a$digits, b$digits)
}

add_magnitude <- function(a, b) {
  out <- rep(0, WIDTH)
  carry <- 0
  for (i in seq_len(WIDTH)) {
    value <- a[i] + b[i] + carry
    carry <- if (value >= B) 1 else 0
    out[i] <- value - carry * B
  }
  if (carry != 0) fail("summary_internal")
  out
}

subtract_magnitude <- function(a, b) {
  out <- rep(0, WIDTH)
  borrow <- 0
  for (i in seq_len(WIDTH)) {
    value <- a[i] - b[i] - borrow
    borrow <- if (value < 0) 1 else 0
    out[i] <- value + borrow * B
  }
  if (borrow != 0) fail("summary_internal")
  out
}

add <- function(a, b) {
  if (a$sign == 0) return(b)
  if (b$sign == 0) return(a)
  if (a$sign == b$sign)
    return(normal(a$sign, add_magnitude(a$digits, b$digits)))
  order <- compare_magnitude(a$digits, b$digits)
  if (order == 0) return(zero())
  if (order > 0)
    return(normal(a$sign, subtract_magnitude(a$digits, b$digits)))
  normal(b$sign, subtract_magnitude(b$digits, a$digits))
}

negate <- function(value) normal(-value$sign, value$digits)

bounded_sum <- function(value) {
  if (compare_magnitude(value$digits, MAX_SUM$digits) > 0)
    fail("summary_internal")
  value
}

magnitude_text <- function(digits) {
  used <- which(digits != 0)
  if (length(used) == 0) return("0")
  top <- max(used)
  value <- sprintf("%.0f", digits[top])
  if (top > 1L) {
    for (i in (top - 1L):1L)
      value <- paste0(value, sprintf("%06.0f", digits[i]))
  }
  value
}

integer_text <- function(value) {
  paste0(if (value$sign < 0) "-" else "", magnitude_text(value$digits))
}

parse_value <- function(raw) {
  bytes <- as.integer(raw)
  if (length(bytes) < 1L || length(bytes) > 21L ||
      any(!(bytes %in% c(43L, 45L, 46L, 48L:57L)))) fail()
  text <- rawToChar(raw)
  if (!grepl("^[+-]?(0|[1-9][0-9]{0,12})(\\.[0-9]{1,6})?$", text))
    fail()
  polarity <- if (substr(text, 1L, 1L) == "-") -1 else 1
  if (substr(text, 1L, 1L) %in% c("+", "-")) text <- substring(text, 2L)
  parts <- strsplit(text, ".", fixed = TRUE)[[1L]]
  fraction <- if (length(parts) == 2L) parts[2L] else ""
  digits_text <- paste0(parts[1L], fraction, strrep("0", 6L - nchar(fraction)))
  digits <- rep(0, WIDTH)
  last <- nchar(digits_text)
  limb <- 1L
  while (last > 0L) {
    first <- max(1L, last - 5L)
    # Only this <=6-digit substring is ever converted to an R double.
    digits[limb] <- as.numeric(substr(digits_text, first, last))
    limb <- limb + 1L
    last <- first - 1L
  }
  result <- normal(polarity, digits)
  if (compare_magnitude(result$digits, MAX_VALUE$digits) > 0) fail()
  result
}

mean_text <- function(total, count) {
  bounded_sum(total)
  if (!is.finite(count) || count != floor(count) ||
      count < 1 || count > MAX_ROWS) fail("summary_internal")
  # Multiplication by the scale is a limb shift, never a large double.
  numerator <- c(0, total$digits)
  quotient <- rep(0, WIDTH + 1L)
  remainder <- 0
  for (i in (WIDTH + 1L):1L) {
    value <- remainder * B + numerator[i]
    if (value < 0 || value >= 10^12 || value != floor(value))
      fail("summary_internal")
    q <- floor(value / count)
    r <- value - q * count
    # Correct a possible one-unit division rounding; prove the final range.
    if (r < 0) { q <- q - 1; r <- r + count }
    if (r >= count) { q <- q + 1; r <- r - count }
    if (q < 0 || q >= B || q != floor(q) ||
        r < 0 || r >= count || r != floor(r) ||
        q * count + r != value) fail("summary_internal")
    quotient[i] <- q
    remainder <- r
  }
  round_up <- 2 * remainder > count ||
    (2 * remainder == count && quotient[1L] %% 2 == 1)
  if (round_up) {
    carry <- 1
    for (i in seq_len(WIDTH + 1L)) {
      value <- quotient[i] + carry
      carry <- if (value >= B) 1 else 0
      quotient[i] <- value - carry * B
    }
    if (carry != 0) fail("summary_internal")
  }
  text <- magnitude_text(quotient)
  text <- paste0(strrep("0", max(0L, 13L - nchar(text))), text)
  split <- nchar(text) - 12L
  paste0(if (total$sign < 0 && any(quotient != 0)) "-" else "",
         substr(text, 1L, split), ".", substring(text, split + 1L))
}

walk_records <- function(source, accept) {
  tail <- raw()
  total <- 0
  repeat {
    block <- tryCatch(readBin(source, what = "raw",
                             n = min(CHUNK_BYTES, MAX_BYTES - total + 1)),
                      error = function(e) fail("summary_unavailable"))
    if (length(block) == 0L) {
      if (length(tail) != 0L) fail()
      return(invisible(NULL))
    }
    total <- total + length(block)
    if (total > MAX_BYTES) fail()
    # This index vector is bounded by one block, never by input row count.
    ends <- which(block == as.raw(10L))
    start <- 1L
    for (end in ends) {
      if (length(tail) + end - start + 1L > MAX_RECORD) fail()
      line <- c(tail, block[start:end])
      tail <- raw()
      accept(line)
      start <- end + 1L
    }
    if (start <= length(block)) {
      if (length(tail) + length(block) - start + 1L > MAX_RECORD) fail()
      tail <- c(tail, block[start:length(block)])
    }
  }
}

summarize <- function(source, condition, expected_rows) {
  if (!(condition %in% c("baseline", "alternative")) ||
      !is.finite(expected_rows) || expected_rows != floor(expected_rows) ||
      expected_rows < 1 || expected_rows > MAX_ROWS) fail()
  groups <- new.env(hash = TRUE, parent = emptyenv(), size = 131L)
  labels <- character()
  rows <- 0
  original <- zero()
  header <- FALSE
  accept <- function(line) {
    if (!header) {
      if (!identical(line, charToRaw("group,value\n")) &&
          !identical(line, charToRaw("group,value\r\n"))) fail()
      header <<- TRUE
      return(invisible(NULL))
    }
    if (rows >= MAX_ROWS || rows >= expected_rows) fail()
    size <- length(line)
    ending <- if (size >= 2L && line[size - 1L] == as.raw(13L)) 2L else 1L
    if (size <= ending) fail()
    body <- line[seq_len(size - ending)]
    commas <- which(body == as.raw(44L))
    if (length(commas) != 1L) fail()
    comma <- commas[1L]
    if (comma < 2L || comma > 65L || comma == length(body)) fail()
    label_raw <- body[seq_len(comma - 1L)]
    bytes <- as.integer(label_raw)
    if (any(!((bytes >= 65L & bytes <= 90L) |
              (bytes >= 97L & bytes <= 122L) |
              (bytes >= 48L & bytes <= 57L) |
              bytes %in% c(32L, 95L, 46L, 45L)))) fail()
    label <- rawToChar(label_raw)
    value <- parse_value(body[(comma + 1L):length(body)])
    if (!exists(label, envir = groups, inherits = FALSE)) {
      if (length(labels) >= MAX_GROUPS) fail()
      labels <<- c(labels, label)
      assign(label, list(count = 0, total = zero(), low = value, high = value),
             envir = groups)
    }
    group <- get(label, envir = groups, inherits = FALSE)
    group$count <- group$count + 1
    group$total <- bounded_sum(add(group$total, value))
    if (compare(value, group$low) < 0) group$low <- value
    if (compare(value, group$high) > 0) group$high <- value
    assign(label, group, envir = groups)
    rows <<- rows + 1
    original <<- bounded_sum(add(original, value))
    invisible(NULL)
  }
  walk_records(source, accept)
  if (!header || rows != expected_rows) fail()
  records <- character(length(labels))
  used_count <- 0
  used_sum <- zero()
  index <- 0L
  for (label in sort(labels, method = "radix")) {
    group <- get(label, envir = groups, inherits = FALSE)
    trim <- condition == "alternative" && group$count >= 5
    count <- if (trim) group$count - 2 else group$count
    total <- if (trim) add(add(group$total, negate(group$low)), negate(group$high)) else group$total
    bounded_sum(total)
    index <- index + 1L
    # Labels contain only the exact ASCII whitelist: no JSON escaping needed.
    records[index] <- paste0(
      '{"group":"', label, '","input_count":', sprintf("%.0f", group$count),
      ',"input_sum_scaled":"', integer_text(group$total),
      '","min_scaled":"', integer_text(group$low),
      '","max_scaled":"', integer_text(group$high),
      '","count":', sprintf("%.0f", count),
      ',"sum_scaled":"', integer_text(total),
      '","mean":"', mean_text(total, count), '"}')
    used_count <- used_count + count
    used_sum <- bounded_sum(add(used_sum, total))
  }
  result <- paste0(
    '{"schema":"educloud.summary-stream-result/v1",',
    '"input_profile":{"id":"summary-fixed6-csv","version":1},',
    '"condition":"', condition, '","input_rows":', sprintf("%.0f", rows),
    ',"input_sum_scaled":"', integer_text(original),
    '","count":', sprintf("%.0f", used_count),
    ',"sum_scaled":"', integer_text(used_sum),
    '","mean":"', mean_text(used_sum, used_count),
    '","groups":[', paste(records, collapse = ","), ']}\n')
  if (nchar(result, type = "bytes") > MAX_OUTPUT) fail("summary_internal")
  result
}

run <- function(arguments) {
  options(warn = 2)
  outcome <- tryCatch({
    if (length(arguments) != 3L ||
        !grepl("^[1-9][0-9]{0,6}$", arguments[3L]) ||
        grepl("[^0-9]", arguments[3L]) ||
        nchar(arguments[3L], type = "bytes") > 7L) fail()
    expected <- as.numeric(arguments[3L])
    if (expected > MAX_ROWS) fail()
    source <- tryCatch(file(if (arguments[1L] == "-") "stdin" else arguments[1L],
                            open = "rb"),
                       error = function(e) fail("summary_unavailable"))
    closed <- FALSE
    on.exit(if (!closed) try(close(source), silent = TRUE), add = TRUE)
    result <- summarize(source, arguments[2L], expected)
    closed <- TRUE
    tryCatch(close(source), error = function(e) fail("summary_unavailable"))
    cat(result, file = stdout(), sep = "")
    flush(stdout())
    list(code = 0L, message = NULL)
  }, summary_invalid = function(e) list(code = 1L, message = "SUMMARY_INPUT_INVALID"),
     summary_unavailable = function(e) list(code = 2L, message = "SUMMARY_INPUT_UNAVAILABLE"),
     error = function(e) list(code = 2L, message = "SUMMARY_INTERNAL_FAILURE"),
     interrupt = function(e) list(code = 2L, message = "SUMMARY_INTERNAL_FAILURE"))
  if (!is.null(outcome$message))
    try(writeLines(outcome$message, con = stderr()), silent = TRUE)
  outcome$code
}

quit(status = run(commandArgs(trailingOnly = TRUE)), save = "no", runLast = FALSE)
