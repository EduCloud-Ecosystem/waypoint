# Waypoint project IDE pilot

This isolated code-server workspace tests a phone presentation of the same
project used from a laptop. The extension invokes the existing editor, Git,
terminal and debugger commands. A phone layout wraps code and reduces panels;
the workspace controls provide 48-pixel actions in a full editor tab.
“Inspect paused values” presents live debugger values in a full-width view with
large step/continue controls. Phone mode opens the terminal at full height,
uses its non-GPU renderer for readable high-density text, and hides its tab list.

Build and launch from the repository root.

```sh
docker build -t waypoint-mobile-ide:pilot ide-pilot
python3 ide-pilot/prepare.py /absolute/new/pilot-directory
sh ide-pilot/start.sh /absolute/new/pilot-directory
```

Open http://127.0.0.1:58771 and use the generated password from server.env.
The port binds to loopback. Keep server.env private. Trust the authored
synthetic project to enable its test task and debugger. Choose “Waypoint:
Open mobile controls” from the command palette, or select Workspace in the
status bar. “Use phone layout” applies reversible workspace settings.

Project files, Git history, editor settings and debugger configuration live
in the selected directory. Closing a browser leaves the server running.
A second browser can open the same directory. The sample starts with a
failing test for missing values. Its README describes the repair exercise.

Stop and remove only this pilot container with the commands below. Retain
the selected directory to preserve work, and rerun start.sh to reopen it.

```sh
docker stop waypoint-mobile-ide-pilot
docker rm waypoint-mobile-ide-pilot
```

The local password protects this operator rehearsal. Institutional deployment
requires Waypoint's existing identity, authorization and workspace lifecycle
integration. The container has no host Docker socket or course data mounted.
It exposes only the synthetic project and its editor state. Notebook exports
and the hosted fallback belong to their existing workflows.

The [physical-device checklist](DEVICE-CHECKS.md) covers the next operator run.
Physical phone keyboard, TalkBack and iOS validation remain distinct from
browser emulation. The supported extension API supplies the mobile command
view; core editor, terminal, Git and debugger layouts remain upstream UI.

## Verification

Run the isolated checks with Node 22+ and Python 3.12+:

```sh
node --test ide-pilot/extension/*.cjs
python3 -m unittest discover -s ide-pilot -p test_prepare.py -v
sh -n ide-pilot/start.sh
```

The browser rehearsal requires Chrome and a locally installed Playwright module.
Set `PLAYWRIGHT_MODULE` to its absolute module directory if it is not on Node's
normal module path. Start a **fresh synthetic workspace**, then run:

```sh
node ide-pilot/verify-browser.cjs /absolute/new/pilot-directory phone
docker restart waypoint-mobile-ide-pilot
node ide-pilot/verify-browser.cjs /absolute/new/pilot-directory laptop
```

The phone run intentionally edits and commits the sample; never run it against
student work. It uses Android browser emulation, touch actions on the controls,
and automated keyboard/clipboard input. It does not emulate the Android keyboard
or TalkBack. The laptop run reconnects to the same files and checks the exact
branch and commit plus another test execution. Logs and screenshots go into
`state/verification/`; keep the generated password file out of reports.

See [VALIDATION.md](VALIDATION.md) for observed results and remaining gates.

The pinned extensions are part of the image. Additional extensions installed
interactively are not preserved when this pilot container is removed. Git
branch/diff/commit and handoff are verified locally; remote Git sign-in and push
require a separate check against an authorized repository.
