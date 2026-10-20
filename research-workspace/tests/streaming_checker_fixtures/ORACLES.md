# Independent literal checker oracles

These are hand-derived values, not outputs from either workload implementation.
All files are tiny synthetic inputs. No workload implementation was read,
imported or invoked to author them.

The `001` group has four values totaling 6; it never trims. The five `NA`
values total 10; alternative removes one -5 and one 9, leaving three values
totaling 6. The `NULL` group cancels +10^12 and -10^12 exactly, retaining
0.000001. The label ` x ` retains its surrounding spaces and has value 2.
ASCII ordering places ` x ` before `001`, `NA`, then `NULL`.

Baseline has 13 used values totaling 18.000001, giving
1.384615461538 at twelve decimal places. Alternative has 11 used values
totaling 14.000001, giving 1.272727363636. Overall means are weighted by
used counts, not averages of group means. The original count and sum stay
13 and 18.000001 in both conditions.

Additional test literals cover equal-valued groups of sizes 1 through 6,
the two half-even ties 1/128 and 3/128 in scaled-input units, negative ties,
the exact +/-10^12 endpoint, negative zero normalization and a deliberately
self-consistent result describing different data. Logical maximum-counter
controls do not represent processing one million rows or 88 MB. Caller
process output collectors remain separately responsible for deadlines and
abnormal interpreter-startup output.
