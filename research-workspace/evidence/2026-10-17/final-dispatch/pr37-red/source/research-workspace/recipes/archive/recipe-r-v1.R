# SPDX-License-Identifier: Apache-2.0
# Reviewed public-data demonstration; group names are validated before dispatch.
args <- commandArgs(trailingOnly=TRUE)
data <- read.csv(args[1], stringsAsFactors=FALSE, check.names=FALSE)
condition <- args[2]
records <- c()
all_values <- c()
for (group in sort(unique(data$group), method="radix")) {
  values <- sort(data$value[data$group == group])
  if (condition == "alternative" && length(values) >= 5) {
    values <- values[2:(length(values)-1)]
  }
  records <- c(records, sprintf('{"group":"%s","count":%d,"mean":%.17g}',
                                group, length(values), mean(values)))
  all_values <- c(all_values, values)
}
cat(sprintf('{"condition":"%s","count":%d,"mean":%.17g,"groups":[%s]}\n',
            condition, length(all_values), mean(all_values), paste(records, collapse=",")))
