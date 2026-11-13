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

## Current product decision

The target is a hosted project IDE with a phone-friendly presentation.
The user's reference is GitHub Codespaces. Students should learn and use
files, repositories, branches, diffs, terminals, tests, breakpoints,
dependencies and notebooks. Those concepts remain available across screen
sizes. The fixed NHANES page is an interaction prototype and its successful
checks apply to that prototype alone.

Use code-server as the first implementation candidate. It retains the
Code OSS workbench and extension model while supporting a self-hosted
browser service. Evaluate required Python, Jupyter and debugger extensions
from permitted distributions on the pinned build. Microsoft's marketplace
availability and exact Codespaces parity are not established by the shared
editor core. [code-server documentation](https://coder.com/docs/code-server/FAQ)

A repo-defined development environment should supply the language tools,
dependencies and tests. Persistent project files and compute belong in the
hosted workspace. Waypoint should reuse its existing identity, workspace
authorization and lifecycle contracts, with the compute broker governing
resource use. Development containers are an environment format; their
configuration alone does not provide multi-user isolation.
[Codespaces environment model](https://docs.github.com/en/codespaces/about-codespaces/what-are-codespaces)

## Phone presentation

| Familiar IDE activity | Phone presentation |
| --- | --- |
| Explorer and search | Full-width file drawer, quick open and search across the project |
| Editing | One active editor, reachable file switcher, symbol/indent row, Undo/Redo and completion |
| Terminal | Full-width terminal with explicit Escape, Tab, Ctrl and arrow controls |
| Problems and debugging | Reachable panel, source-linked errors, breakpoint and step controls |
| Source control | Full-width diff, explicit stage/unstage, commit message, branch and sync actions |
| Notebook analysis | Notebook editor with expandable outputs and plot inspection |
| Preview | Explicit switch to the running app through the authenticated workspace |
| Resume on a laptop | Same persistent repository and environment, with the full workbench layout |

Use the same commands, document models, filesystem and extension host behind
both layouts. Investigate supported configuration and extension APIs first.
A substantially different workbench layout may require a maintained source
patch. Scope that cost before committing to a fork. A separate custom editor
would also need reliable integration with language services, notebooks,
debuggers, terminals and extension UI.

## First complete IDE pilot

Create an isolated code-server workspace from a small Python repository with
a module, tests, a notebook and a documented environment. Exercise opening
files, editing, running a failing test, fixing the code, stepping through a
breakpoint, viewing a notebook result, reviewing a diff and committing on a
branch. Reconnect from a laptop to the same project. Then measure the same
workflow on a phone with its keyboard and TalkBack, including interruption
and recovery.

The existing notebook prototype supplies evidence for touch controls and
draft interactions. It does not satisfy this IDE pilot. Evaluate code-server
against Codespaces and the unmodified workbench on the same tasks. Continue
if the phone presentation substantially improves task completion while
preserving file integrity and familiar IDE operations. Shelf the custom
presentation if comparable configuration provides the same benefit or if
maintaining it compromises compatibility and reliability.

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

## Prior prototype checkpoint

The workspace iteration passed the 15 bundle checks and the prior emulated
execution/export rehearsal. Additional rendered checks covered Undo through
an empty editor, Redo, indentation insertion, editor expansion and Escape,
automatic save/reload, 320-pixel layout, and zero axe A/AA violations in the
checked state. Screenshots were inspected at phone and desktop widths.
The physical device evaluation remains pending. The hosted project IDE is
the next implementation target and is not implemented by this branch.
