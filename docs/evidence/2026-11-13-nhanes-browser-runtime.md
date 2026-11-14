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

## Cold loopback run and device checks

The [cold-run evidence](2026-11-13-nhanes-browser-cold.json) extends the
rehearsal at Waypoint `6f29f4a3fd60062713b1ecb4f005e11312ca6376`. It uses the
same starter and synthetic solution. Versions remained JupyterLite core and
kernel 0.8.7, Pyodide 314.0.6, Python 3.14.2 and pandas 3.0.2.

The earlier request trace identified 24 Pyodide files and a separately fetched
`comm==0.2.3` wheel. Stage these 25 files (25,702,484 bytes) before opening the
browser. The evidence records exact acquisition URLs, sizes and SHA-256
digests. Verify the Pyodide wheel hashes against its release lock and the
`comm` wheel against its version metadata; verify all recorded hashes when
restaging. Build the previous static site with `--piplite-wheels <comm-wheel>`.
Copy the recorded Pyodide files under the served `pyodide/` directory, set
`pyodideUrl` to `/pyodide/pyodide.mjs` and set `disablePyPIFallback` to true in
the Pyodide kernel's `litePluginSettings` entry in `jupyter-lite.json`. Retain
the local wheel index generated by the build. See the
[Pyodide kernel configuration](https://jupyterlite-pyodide-kernel.readthedocs.io/en/stable/index.html).

Run from a fresh Chrome profile, with the site bound to loopback and an HTTP
proxy that denies non-loopback requests. Check the guard with a reserved
external hostname in a separate tab, then open the notebook. Record browser
context requests including worker requests and the proxy's refused connections.
The desktop and throttled emulation recorded no non-loopback application
requests; Chrome background attempts to Google hosts were blocked. This is
evidence for cold startup from a local server, not an offline reload test
with the server absent or a claim about all browser or OS traffic. Asset
acquisition itself required the network. No runtime asset was redistributed.

| Check | Result |
| --- | --- |
| Desktop cold execution | Chrome 157 on macOS produced the same 192-row CSV as native Python and exported all four files. Time from navigation through fixture entry and result was 10.01 seconds in this run. |
| Pixel 7 emulation | 412 CSS-pixel viewport, 4x CPU slowdown, 150 ms latency, 200,000 bytes/s down and 90,000 bytes/s up. The open file panel blocked editor interaction. Collapsing it allowed execution and all four exports, with byte-identical outputs to desktop. Time to result was 44.69 seconds after allowing startup longer than the failed 30-second attempt. Long code lines still clipped. Timings are single observations of the harness, not device performance guarantees. The export used automated context menus, not touch-only operation. |
| Loss of access to the original profile | A fresh empty profile started with the 48-byte CSV template. Importing the four independent downloads, opening the notebook and explicitly saving before redownload recovered all four byte for byte. This simulates unavailable browser storage; it does not exercise eviction, quota or the browser's private mode. |
| Keyboard and automated accessibility | Tab reached “Skip to main panel,” Enter moved focus to a notebook Markdown cell, and the keyboard save shortcut was exercised. axe-core 4.10.3 flagged four rule categories on desktop and emulation: disallowed ARIA attributes, unnamed input fields, missing required ARIA children and nonfocusable scroll regions. It also requested manual review of prohibited ARIA attributes. The emulated layout had more affected scroll regions. This is not a full keyboard-only or assistive-technology test. See the [axe-core project](https://github.com/dequelabs/axe-core). |

Preserved failed attempts include the initial navigation race in the proxy
probe, an abandoned headless-browser stall, an invalid focus setup after a
successful desktop export, unsupported device-descriptor metadata in the
harness, slow startup exceeding 30 seconds, and an open sidebar intercepting
editor interaction. These attempts, the runtime lock, raw traces, screenshots,
and exports are retained in the ignored local output directory
`course-workspace/output/browser-cold-rehearsal-v1/`. The successful runs
closed their browser profiles, loopback servers and deny proxies.

Checkpoint: the reused Waypoint checkout is on
`codex/browser-cold-rehearsal-v1`, based on the merged rehearsal record at
`6f29f4a`. This changes only the evidence record and its cold-run JSON. Keep
the browser route a developer candidate with the hosted fallback. Next,
address the narrow layout and keyboard/ARIA issues, then repeat with a real
low-end phone or tablet, human screen-reader checks and a touch-only export.
Hosted authorization, committed submission and inference qualification
remain separate gates.

## Narrow layout and keyboard candidate

The optional [Waypoint adapter](../../browser-candidate/README.md) uses the
pinned Lab controls and existing notebook commands. It keeps the file panel
closed by default on narrow screens, wraps code, labels cell editors and
scroll areas, and exposes native Run, Save and per-file Download controls.
The hosted workspace remains the fallback, and the original bundle and release
are unchanged.

[Observed results](../../browser-candidate/REHEARSAL.json) bind the adapter
sources, cold runtime inventory, and export hashes. On desktop Chrome, Tab and
Enter reached notebook actions; Arrow Down and Enter reached the solution
editor. Run, Save, dialog downloads and Escape were exercised with keys. On
throttled Pixel 7 emulation, the fresh notebook opened without the file panel;
tap actions edited (with automated text insertion), ran, saved, toggled files
and downloaded each artifact. At 412 CSS pixels the notebook was 336 pixels
wide and the wrapped solution editor had no horizontal overflow. Both produced
the same 192-row CSV as native Python, and all four exports matched across runs.
A clean profile recovered the CSV and both Markdown files byte for byte.
Opening and saving the imported notebook preserved its full parsed JSON, but
JupyterLite reserialized its bytes. The strict notebook byte gate failed;
retain the original download. The deny proxy blocked external browser traffic and the
app made no non-loopback requests during the recorded runs.

Axe-core 4.10.3 reported zero violations for the scanned A/AA rules in the
desktop and emulated notebook, open file panel and download dialog states.
It still requested manual review of labels on upstream shadow-DOM toolbar
hosts. This result is limited to those states and rules. See the
[physical device and assistive technology runbook](../../browser-candidate/DEVICE-CHECKS.md)
for the remaining checks. The emulation did not exercise a device's OS
keyboard or an actual screen reader.

Checkpoint: `codex/browser-accessibility-candidate-v1` in the reused Waypoint
browser worktree, starting from `504009aff30d97fde1ec472b64d08cd3cabb211c`.
Changes are limited to the opt-in adapter, documentation and bounded evidence.
The 15 existing packager tests passed, and preparation rejected changed starter
bytes, extra files, and an existing destination. Raw browser traces, failed
attempts and rendered evidence are retained under ignored
`course-workspace/output/browser-accessibility-candidate-v1/`. Test servers
and browser sessions were closed. Next run the prepared checks with physical
devices and human assistive technology use, without promoting the candidate.
