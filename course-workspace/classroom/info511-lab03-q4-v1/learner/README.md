# INFO511 Lab 3 Q4: synthetic rehearsal

Task version: `info511-lab3-q4-synthetic-v1`. This is a prepared engineering
activity, not an adopted policy for a live course. Use only the three fictional
countries in `data/info511-inflation-synthetic.csv`. These are instructor-created
values, not observations from the original course CSV.

## Your task

Open `lab3-q4.ipynb` with the existing Python 3 kernel. Before optional help,
replace the two labeled prediction cells with your own expected schema and
observational unit. Read the source into `country_inflation`. Use `melt` or
`pivot_table` to create a separate `tidy_inflation` table; preserve the input.
The result has columns `country`, `year`, `annual_inflation`: one row per country
and year, integer four-digit years, every original value and every missing cell.
This fixture has 9 country/year pairs and 3 missing inflation values.

Write your own transformation and independent checks. Save the tidy table as
`tidy-inflation.csv` without a row index. Document your reasoning in
`explanation.md`, at most 150 whitespace-separated words, written by you.
No assessed pipeline or explanation is supplied in this learner package.

## Assistance and the equivalent non-AI route

You can complete the entire task with the notebook, your course notes, and
approved documentation for reshaping, missing data and CSV export. No AI or
optional monitoring is required and the draft rubric is the same.

Fill `assistance.md` with exactly these first three lines, replacing the choice
and summary yourself:

```text
Task version: info511-lab3-q4-synthetic-v1
Declaration: no_ai
Summary: Briefly describe the resources you used.
```

Choose `no_ai` or `ai_assisted` accurately. A no-AI declaration is valid. If you
used assistance, describe its scope briefly; do not paste tutor conversations,
private reflections, monitoring alerts, access tokens or identifying details.
An incomplete declaration is a reminder to finish, not an accusation.

Optional tutoring is not activated by these files. It requires the separately
reviewed B1 policy and covered P1 route. If enabled later, it is hints-only:
no complete assessed solution, assessed narrative or grade decisions. If help
is unavailable, continue with the non-AI materials and the same notebook.

CW1 adds no monitoring or event collection. Any future optional B1 monitoring
requires its own notice and explicit permission. Pausing optional monitoring
does not turn off required access controls or disclosed operational logs.

## Saving is not submitting

1. Save all four files: `lab3-q4.ipynb`, `tidy-inflation.csv`, `explanation.md`,
   `assistance.md`. Remove unintended notebook output or private material before
   selecting it; the bridge preserves saved bytes and does not redact them.
2. Use **Submit saved files to Cairn** in the existing Jupyter interface. Select
   only those four files and choose **Prepare saved files**. Alternatively,
   download the four saved files and use Cairn's explicit device upload.
3. Sign in separately to Cairn with your synthetic rehearsal account. Receive
   the files, choose the correct assignment, and select **Preview submission**.
   Check the listed filenames, sizes and hashes. Transfer and preview are not
   submission.
4. Select **Confirm submission**. Wait for **Committed - repository receipt
   verified** (the existing UI may use typographic punctuation). Retain the
   receipt ID and commit revision through Cairn's authenticated interface.
   Pending, conflict or verification-needed states are not success. If a
   confirmation response is lost, inspect/recover the same preview/receipt;
   do not create a fresh submission to conceal uncertainty.
5. Saved changes after that snapshot are not part of the submitted commit.
   A later intentional submission needs a new preview and confirmation.

Keep each file below 256 KiB and the four files below 1 MiB for this task's
review boundary. The general bridge has larger limits; those do not widen the
assessment extractor. A completeness reminder establishes only presence and
format, not correct science, independent authorship, grade or successful submission.
