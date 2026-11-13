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

## Decision

Use the Waypoint view for short, touch-first assignments built around a
known notebook and its exports. Keep Notebook 7 as the general-purpose
hosted candidate, where arbitrary notebooks, additional packages, plots
and server-side persistence matter. Keep the current hosted fallback and
the original notebook download available.

A local comparison used stock Notebook 7.6.5 and the Waypoint candidate in
Chrome 157 with a 412 CSS-pixel Pixel 7 emulation. Both supported edit and
undo, rendered a table, and produced the same 192-row fixture CSV. For
Notebook 7, a disposable copy used a runtime-independent print marker.
Both pages fit the viewport. In Notebook 7, the default code editor
required horizontal scrolling for long lines, and Run All Cells was inside
the Run menu. Waypoint wrapped the code, used named fields for editing,
and exposed a direct Run all cells action. This comparison covers default
UI behavior in emulation. Device screen readers, the on-screen keyboard,
interrupted sessions and hosted submission still need paired checks.

Build the Callisto and Juno ideas into the existing web view in this order:
a small symbol and indent row with visible undo, controls to move between
the editor and its output, and draft recovery with a clear save state.
Keep the standard notebook format. An installable web app is a later option
if offline reopening and runtime caching can be verified. Consider native
clients if testing establishes a need for deep file and share integration
or on-device Python packages unavailable in the browser.

For a field assignment, a phone can collect a dataset at the observation
site. For example, import a [phyphox export](https://phyphox.org/docs/) from
an accelerometer experiment and inspect it on site. Use an explicit import
action and a sample dataset for students without the relevant sensor.

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
