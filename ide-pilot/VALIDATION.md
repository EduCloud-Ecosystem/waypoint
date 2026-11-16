# Code-server mobile IDE pilot change record

## Requested outcome and design

The user requested a mobile-friendly Codespaces-style IDE with the same
persistent workspace across phone and laptop. Editing, tests, debugging and Git
must use real project tools. The original notebook and hosted fallback must
remain unchanged.

Reuse code-server/Code OSS and its existing filesystem, Git, terminal, extension
host and Python debugger. Extend it through the supported VS Code extension API
with reversible phone settings, larger command controls and a focused paused-
variable view. The variable view reads the same Debug Adapter Protocol session;
it neither runs a second interpreter nor stores a second copy of project files.
Native views remain available for advanced debugger features and normal desktop
work. This avoids maintaining a fork of the editor or its CSS.

The earlier notebook-only approach did not provide this full project workflow.
It remains separate. This change adds an isolated operator pilot and does not
replace a course entry point, deploy a tenant, or alter hosted identity/routing.
The bind-mounted synthetic project is the shared workspace for this pilot; a
connection to an existing institutional learner volume has not been deployed.

## Implementation boundary

- code-server 4.145.2 / Code OSS 1.145.2; base image digest pinned in Dockerfile.
- Python extension 2026.4.0, debugger 2026.6.0, environments 1.42.0.
- Python and Git supplied by the container image's Debian packages.
- One loopback-only, password-protected container; 2 CPU and 2 GiB limits.
- Project/Git history and editor state survive browser and container restarts.
- Phone layout restores the previous workspace configuration values. Native
  pane visibility can be reopened using normal IDE controls on a laptop.
- Focused inspector shows at most 100 top-level values in the first local or
  non-expensive scope of the top frame. Nested objects, watches and other frames
  remain in the native debugger. Running sessions clear paused values.
- Only synthetic fixture source and identity are used. No student data or host
  Docker socket is mounted. Credentials are created atomically with mode 0600.

## Verification evidence

Final packaged run: **PHONE_WORKFLOW_OK** and **LAPTOP_WORKFLOW_OK** on
2026-11-13. See [the immutable result and source hashes](evidence/result.json).
The 412-pixel phone run covered edit/save, failing-to-passing tests, live paused
variables, step/continue, Git branch/diff/commit, and 320-pixel controls with
48-pixel minimum height. A container restart preceded the new 1440-pixel laptop
session; the exact branch/commit and passing tests matched. The synthetic Git
working tree was clean. Remote Git authentication/push was not exercised.

Rendered inspection: [focused debugger](evidence/phone-debug.png),
[phone terminal/Git](evidence/phone-git.png),
[narrow controls](evidence/phone-controls-320.png), and
[laptop handoff](evidence/laptop-handoff.png).

Both protected notebook copies retained SHA-256
`133358bd417c04088383b00f3f2ff3c40463a77f4829f251c559b9e2dbc380ac`.
Existing hosted fallback and deployment source have no diff. Local raw
artifacts, including earlier failures, are retained under the ignored
`course-workspace/output/code-server-pilot/` directory. They are not classroom
or production evidence.

Focused checks: two Node tests cover layout restoration, action allowlists,
returning to the editor, real DAP request shapes and clearing paused values.
The Python preparation test verifies private credential permissions, refusal to
overwrite, a clean initial Git repository and the intentionally failing exercise.
CI runs these checks, browser-harness syntax and shell syntax.

The browser harness exercises the extension by touch in Pixel 7 emulation,
uses browser keyboard/clipboard operations to edit the source, and verifies
on-disk bytes. It reads actual debugger variables and steps/continues, runs
unit tests in the IDE terminal, creates a Git branch/diff/commit there, and
reconnects from a new laptop-sized browser after a container restart.

Failures retained during development:

- Initial image build lacked ownership of the extension directory; fixed owner.
- Reordering image layers omitted the custom extension from the generated
  extension index; fixed registration order and added an image-build assertion.
- Initial automation used wrong platform shortcuts, selected an ambiguous
  filename, treated a wrapped visual line as a whole source line, and typed a
  Git command into a pager. Repaired using named IDE actions, exact selection,
  clipboard paste and explicit no-pager commands. The final diff must contain
  only the intended denominator repair.
- An initial narrow-layout check assumed a particular webview frame URL;
  replaced with inspection of rendered button roles across frames.
- Stepping initially hid the focused inspector; added retained webview state,
  reversible focus-on-break settings and return-to-inspector behavior. The
  harness now waits for the debug session to end before its next UI command.
- GPU terminal rendering enlarged text on the high-density Android viewport;
  phone mode now uses the non-GPU renderer and hides the terminal tab list.
  Final phone screenshots show readable text. Panel hiding happens before
  asynchronous setting writes so a later action is not unexpectedly hidden.
- Native phone debugger panes were visually cramped despite functional debug
  success; added the focused variable view and full-height terminal opening.

## Remaining qualification

[Physical-device checks](DEVICE-CHECKS.md) are prepared but NOT RUN. No claim is
made about the Android on-screen keyboard, TalkBack, iOS, sustained phone-only
work, collaboration, reconnecting an unsaved buffer, or classroom capacity.
Institutional auth, per-student allocation/lifecycle, backup and routing must use
Waypoint's existing contracts before any classroom deployment. This local
password is not an institutional sign-in integration.

## Checkpoint

Checkout: `/Users/gregchism/dev/.educloud-worktrees/waypoint-browser-notebook-bundle-v1`.
Branch: `codex/code-server-pilot`, started from main
`f60c9489b843d68a42288ee4d9cf01cfa16686f8`.
Change scope: `ide-pilot/`, `.github/workflows/ide-pilot.yml`, `CHANGELOG.md`.
No original notebook, fallback or existing deployment source is edited.

The final source remains a pilot candidate. Next action: review the PR and run
DEVICE-CHECKS.md on the Pixel before choosing it as a primary mobile IDE. The
local container remains running on loopback port 58771 with the accepted
synthetic workspace; original workflow servers were not stopped. Runtime
artifacts live under `course-workspace/output/code-server-pilot/acceptance-v5/`.
