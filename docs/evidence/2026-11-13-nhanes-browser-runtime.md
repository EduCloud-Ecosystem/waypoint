# NHANES browser runtime rehearsal, November 13, 2026

A real browser kernel executed the existing INFO511 NHANES reference workflow
against the reviewed public CDC subset. It produced the same CSV bytes as native
Python. Save, reopen, and export succeeded; recovery into a fresh browser profile
required opening and saving the imported notebook before downloading it again.
This is a bounded developer rehearsal. Keep the hosted workspace as the fallback
and do not promote the source packager's qualification flags.

The governing [browser bundle record](../../course-workspace/classroom/BROWSER-BUNDLE.md)
defines the remaining gates. The [machine-readable evidence](2026-11-13-nhanes-browser-runtime.json)
records exact source and artifact digests without including notebook answers,
learner identities, browser profiles, logs, or course submissions.

| Gate | Observed result and limit |
| --- | --- |
| Runtime and correctness | Chrome 157.0.8103.51 on macOS, JupyterLite core/kernel 0.8.7, Pyodide 314.0.6, Python 3.14.2, pandas 3.0.2. The browser executed both code cells without notebook error outputs. The result had 192 rows and 58 missing readings. CSV bytes match native Python 3.14.5 with pandas 3.0.6. The existing hash-pinned Belay checker passed the native, browser and restored CSVs. Native Python is a comparison, not an exercise of the hosted container or an authorship judgment. |
| Save and reopen | Explicit save; closed the browser and reopened the same isolated persistent profile. The notebook's executed outputs and the generated CSV remained available. This does not cover browser eviction, quota exhaustion or private browsing. |
| Export and recover | Downloaded all four named artifacts through the file browser. The original notebook export validated as nbformat 4. Uploaded all four into a new folder in a separate clean profile. CSV and Markdown bytes matched on immediate redownload. The notebook initially redownloaded as a JSON string containing base64, not an nbformat object; decoding it reproduced the original bytes. It opened with the original outputs. After **open, explicitly save, then download**, all four files matched the original exports byte for byte. The Markdown files were unchanged starter placeholders, not evaluated student work. |
| Network | The captured cold run made 24 GET requests to jsDelivr, one to PyPI and two to files.pythonhosted.org for runtime/package resources. Other captured requests were loopback, including service-worker drive requests. This is an observation of one run, not a network policy, complete telemetry audit, offline guarantee or authorization for private data. |
| Layout and access | Desktop notebook and outputs were inspected. At a 390 by 844 desktop viewport the open file browser left the notebook nearly unreadable; collapsing it improved the layout, but code and menus still needed more space. A keyboard save shortcut was exercised. No actual mobile device, screen reader, full keyboard-only flow, or human accessibility review was run. |
| Fallback and submission | The packager used the reserved `workspace.example.invalid` placeholder solely as metadata. No institutional login, hosted capacity, learner identity, selected-file submission, or committed receipt was exercised. No model, cloud instance or real student data was used. |

## Reproduce the bounded rehearsal

1. Start at Waypoint `aab24cf32987e6593715e84b24050ccc8fb64746`. Build and
   verify the `nhanes-v1` starter archive with the packager documented above.
   Extract only that verified archive into a fresh, ignored staging directory.
   Keep the reference solution and checker outside the served site.
2. In an isolated Python 3.14.5 environment, install
   `jupyterlite-core==0.8.7`, `jupyterlite-pyodide-kernel==0.8.7`,
   `jupyter-server==2.17.0` (build-only contents indexer), and
   `pandas==3.0.6` for the native comparison. Run
   `jupyter lite build --contents <staging> --output-dir <site>`.
   Serve the static site on loopback only, using a fresh browser profile.
   See [kernel setup](https://jupyterlite.readthedocs.io/en/stable/howto/configure/kernels.html)
   and [kernel file access](https://jupyterlite.readthedocs.io/en/stable/howto/content/python.html).
3. Open the reviewed notebook and select Python (Pyodide). In the private
   profile only, put the existing synthetic reference notebook's solution cell
   into the empty solution cell. Run all cells, record actual versions and
   outputs, save, and export the four named files. Run the reference cells
   natively against copies of the same source bytes. Compare CSV bytes and check
   with the digest-pinned checker and source files in the operator binding.
4. Close and reopen the original browser profile. Restore the downloads into
   another clean profile. Keep the originals, compare all redownloads, and
   verify that the notebook opens and that a save followed by download produces
   a valid notebook with matching bytes. Inspect cold requests and narrow
   layout separately. Do not use this procedure as a submission route.

The initial build failed without the build-only Jupyter Server dependency.
The restore console logged two 404s for static indexes of browser-created
folders; those folders and files were still visible in local storage. The
failed initial build, failed direct notebook roundtrip, and rendered evidence
remain in ignored local output, alongside the successful artifacts. Both
browser sessions and the loopback server were closed. The package freeze is
observed, not a hash-locked rebuild or a redistribution review.

Checkpoint: the dedicated branch `codex/browser-runtime-qualification-v1` starts
at the exact main revision above and changes only this report and its JSON.
The evidence directory is
`course-workspace/output/browser-runtime-qualification-v1/` in the reused
Waypoint browser bundle checkout. Published prerelease bytes were not replaced.

Next: test a version-pinned cold build with dependencies served locally and
verify that it makes no external requests. Repeat recovery and layout checks
on a low-end device, including loss of local storage and keyboard/screen-reader
use. Independently qualify the real hosted fallback and submission before
offering the browser route to learners. The endpoint experiment remains a
separate workstream requiring its own admission and review.
