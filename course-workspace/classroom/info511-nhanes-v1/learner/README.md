# INFO511: NHANES joining and tidy readings

This assignment uses selected public CDC NHANES August 2021–August 2023 data.
Open `nhanes-wrangling.ipynb` in Jupyter and run the first code cell to load the two tables. The deliberately selected 64-person
subset is not representative. Make no population, causal or medical claim.

Predict the input/joined/output schemas and observational units in the labeled
notebook cells before optional help. A valid no-AI route uses the same task and
rubric. Private monitoring is optional and is never grading evidence.

## Task

Read `data/DEMO_L_selected.csv` (`SEQN`, `RIDAGEYR`) and
`data/BPXO_L_selected.csv` (`SEQN`, `BPXOSY1`, `BPXOSY2`, `BPXOSY3`). These are
unaltered selected columns and records, with empty numeric fields representing
missing measurements. Both supplied keys are unique. The anchor includes ages
18–79 and excludes the age-80-plus topcode; this is not a definition of all adults.

Use DEMO as the left anchor. Perform a `merge` on `SEQN`, explicitly validating
`one_to_one`. Distinguish a matching BPXO record from no BPXO record. A matching
record can still have missing readings; neither status tells you why data is absent.
Use `melt` to retain all three readings per participant. Save a new result named
`tidy_nhanes`, preserving both input frames.

The exact CSV column order is:
`SEQN,RIDAGEYR,reading,systolic_mm_hg,bpxo_match`.
`reading` is integer 1, 2 or 3; `bpxo_match` is `matched` or
`no_BPXO_record`, describing membership in the supplied BPXO table only.
The output observational unit is one selected participant and one reading.
Retain every anchor, every observed value and every missing position, including
three missing readings for each unmatched participant. Do not average, impute,
drop missing rows, treat zero as missing, or apply generic 7/9/77/99 recodes.
The selected systolic variables are numeric measurements, not categorical codes.

Write independent checks: key/cardinality and age preservation, one row per
`SEQN`/reading, join membership, missing masks, scalar source comparisons and a
lossless pivot roundtrip. Demonstrate unchanged inputs. The formative checker is
feedback, not a full grade, authorship judgment or proof of kernel execution.

## Save and submit explicitly

Submit exactly `nhanes-wrangling.ipynb`, `tidy-nhanes.csv`, `explanation.md` and
`assistance.md`. Execute your notebook in its real kernel, inspect errors, save it
and write the CSV without a row index. Explain the units, join choice, missingness
and independent checks in at most 150 words. Declare `no_ai` or `ai_assisted` with
a brief summary; omit private hints, coaching conversations and monitoring records.

In Studio choose **Submit work**, select the four saved files, then **Prepare saved files**. Choose **Continue in Cairn**, review the changes and confirm. Look for the receipt with your submitted revision. If no receipt appears, check your submission history before retrying. If the tutor expires, keep working in the notebook. For help with access or submission, contact your instructor through the course support channel and describe the step that stopped.

## How your work is reviewed (10 points, proposed for instructor review)

| Points | Evidence |
|---|---|
| 4 | The CSV preserves participant IDs, ages, readings, missing positions and file membership. |
| 3 | Your notebook independently checks the join, keys, missing values, roundtrip and unchanged source tables. |
| 2 | Your saved code reproduces the CSV. Your short explanation identifies the units, join choice, and limits of interpreting missing data. |
| 1 | Your assistance declaration describes how you worked. Choosing no AI earns the same credit. |

The automated CSV result covers the first four points. Your instructor reviews the remaining evidence.

Source documentation: [CDC demographics](https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/DEMO_L.htm),
[CDC oscillometric blood pressure](https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/BPXO_L.htm).
No national estimates are requested; survey weights/design would require a
different analysis contract.
