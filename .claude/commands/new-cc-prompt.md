---
description: Scaffold a new CC-* Claude Code prompt file with the next free number in this repo's series, after checking both the shared registry and this repo's own docs/prompts/ directory for drift.
---

Scaffold a new Claude Code prompt for this repo, numbered correctly against
the platform-wide CC-* namespace, and update the registry in the same pass.

Argument: `$ARGUMENTS` is the prompt's working title/slug (e.g. "fix student
landing redirect"). If empty, ask what the prompt is for before proceeding —
don't invent a title.

## Steps

1. **Read the registry.** Read `../educloud/DOCUMENTATION.md` and find the
   "Prompt-number namespace" section. It lists the highest number used so
   far in each series (`CC-P*`, `CC-CA*`, `CC-B*`, `CC-O*`, `CC-W*`). Note
   the number for *this* repo's series specifically. If the relative path
   `../educloud` doesn't resolve, find the actual sibling directory first —
   don't guess.

2. **Cross-check ground truth.** List this repo's own `docs/prompts/`
   directory directly (e.g. `ls docs/prompts/`) and find the actual highest
   number in this repo's series present on disk. The registry can drift
   from reality — **the directory listing wins if the two disagree.**

3. **Pick the number.** Use one higher than the max of (registry number,
   on-disk number). If they disagreed, flag that discrepancy explicitly in
   your report at the end rather than silently reconciling it.

4. **Write the prompt file** at
   `docs/prompts/CC-<series><n>-<slug>.md`, where `<slug>` is a kebab-case
   version of the title. Use this shape:

   ```markdown
   # CC-<series><n> — <real, specific title, not a placeholder>

   *Claude Code prompt. Authored [where/when — e.g. "in Cowork, 2026-08-09"],
   from [what triggered this: a finding, an instruction, a prior prompt's
   report]. What's already known/verified: [...]. What this prompt needs to
   resolve: [...].*

   ## 1. Read first

   [Files/docs the executor must read before touching anything, in the
   order that matters.]

   ## 2. [First substantive task]

   ...

   ## N. Report

   Report back:
   - [Exactly what must be confirmed/measured/decided]
   - Any divergence from the happy path
   - Anything that couldn't be verified and why
   ```

   Never omit the Report section — it's the last numbered section, always.

5. **Update the registry.** Edit `../educloud/DOCUMENTATION.md`'s
   "Prompt-number namespace" line to reflect the new highest number for
   this repo's series (e.g. `CC-CA* Cairn (CA1–CA4 used)` becomes
   `(CA1–CA5 used)`). Do this in the same pass, not as a follow-up — a
   registry update that doesn't happen immediately tends not to happen.

## Report

After scaffolding, report:

- The new file's path and number
- Whether the registry and the on-disk directory agreed before this change
  (and if not, what the discrepancy was)
- Confirmation the registry was updated
