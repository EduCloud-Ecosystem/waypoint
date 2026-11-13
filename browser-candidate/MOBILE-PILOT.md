# Mobile notebook pilot

The physical Pixel rehearsal of the Lab adapter confirmed that a touch edit
could be made and undone with the system keyboard. The operator found the
view cramped and paused before reading cell outputs with TalkBack. The new
page reuses the reviewed NHANES bundle and pinned Pyodide assets. It gives
the notebook the full page width and uses native multiline editors, headings
and table markup. The hosted workspace and both original downloads (the
reviewed starter and the preserved rehearsal export) retain their bytes.

| Option | Role in the comparison |
| --- | --- |
| [Callisto](https://callistoapp.eu/) | Android usability benchmark. Opens and saves standard notebook files and can execute Python on the device. The developer lists the symbol keyboard, pandas and optional remote Jupyter in the paid unlock. |
| [Juno](https://juno.sh/juno-notebook-workspace/) and [Juno Connect](https://juno.sh/junoconnect/) | iPhone/iPad benchmarks for configurable keyboard rows, reachable cell actions, plot inspection, device files and optional remote execution. |
| [Carnets](https://holzschu.github.io/Carnets_Jupyter/) | Open-source iOS local-execution comparison with an extended keyboard, notebook folder access and bundled pandas. |
| [Hosted Notebook 7](https://jupyter-notebook.readthedocs.io/en/stable/notebook_7_features.html) | The Mobile IDE Pilot's broader coursework candidate. The compact view uses server-side Python and needs connectivity. Waypoint's current course image and spawner use Lab. Compare Notebook 7 separately against the same assignment and existing handoff with no change to the default workspace. |
| [CyVerse VICE](https://docs.cyverse.org/deployment/06-applications/vice/) | Hosted architecture reference. A launch can provision an interactive container with a mounted Data Store home. Its [featured JupyterLab flow](https://learning.cyverse.org/de/vice/quick-jupyter/) does not establish touch or TalkBack usability. |
| [marimo](https://docs.marimo.io/guides/exporting/webassembly_html/) | Relevant for touch-first interactive lessons and browser execution. Its Python source format and reactive execution would require changes to the current notebook assignment. |

Evaluate the custom view first for opening, reading, editing and exporting
this small assignment. Keep hosted Notebook 7 as the comparison for plots,
additional packages, persistent server-side work and submission. The
controls can be evaluated independently of where Python executes. Callisto
and Juno are benchmarks and optional tools until their accessibility, file
round trip, packages and institutional sign-in are tested. No native app
integration is required for the browser view.

Candidates for a second iteration include a small symbol and indent row
with visible undo, an output view that stays readable when the keyboard
opens, and a draft that can be reopened after an interruption. Measure
taps, time to resume, and lost work. For field coursework, a phone can also
collect a dataset where the observation happens. For example, import a
[phyphox export](https://phyphox.org/docs/) from an accelerometer experiment
and analyze it in the notebook. Use an explicit import action and a sample
dataset for students without the relevant sensor.

## Checkpoint

2026-11-13. Branch `codex/mobile-notebook-view` started at GitHub Waypoint
main `f60c9489b843d68a42288ee4d9cf01cfa16686f8` in the existing browser
rehearsal checkout. The change touches only the optional staged browser
page, its preparation and documentation. The new route is `/mobile/` and
requires `prepare.py --mobile`.

Pixel 7 Chrome emulation at 412 CSS pixels exercised textarea edit and
undo, public inputs and the synthetic reference run, a semantic table,
save/reload, four working exports and the byte-identical starter download.
The 192-row CSV matched the reference SHA-256
`0f15ad84fbf6441dbe3c6a3c7d850dcae080410d92d392c07754a3ef1ad1e8b8`.
Python errors were visible and the stale CSV download was disabled.
The checked state had zero axe A/AA violations. The test context blocked
external application requests and recorded only same-origin requests.
The physically tested editor and TalkBack controls were from the prior Lab
view. The new layout, system keyboard and TalkBack reading order still
require verification on the Pixel. The comparison requires the same
touch-only edit, table reading, export/reopen and interruption tasks on
physical Android and iOS. Server-backed submission and fallback
authorization remain outside this local run.
