# Physical device qualification

Status: NOT RUN for this code-server pilot. Browser emulation does not establish
Android keyboard, touch selection, TalkBack or sustained-use acceptance.
Use only the generated synthetic project. Preserve its initial failing example
or prepare a second directory if the automated repair is already committed.

Keep the loopback-only binding. For a USB-connected Android device, an operator
can forward device port 58771 to host port 58771 using `adb reverse`. Open
`http://127.0.0.1:58771` on that device and enter the private generated password.
Do not publish that password or expose this local pilot to the network.

Run these one at a time and record device/browser versions, announced text,
focus location, outcome, and a screenshot when useful:

| Check | Acceptance |
| --- | --- |
| Open Workspace, choose phone layout | Controls fit the screen; large buttons can be reached by touch without horizontal page scrolling. |
| Open analysis.py and edit with the on-screen keyboard | Cursor and current line stay visible; type one character, undo it, save; rotate and verify no lost edit. |
| Select, copy and paste | Controls act on the intended file and selection; clipboard denial produces a recoverable result. |
| Run tests and debug | Read the intentional failure, set a breakpoint, inspect values, step and continue; switching views preserves code and cursor. |
| Git and laptop handoff | Review the exact diff, create a branch, commit; open the same server on a laptop and verify the branch, commit, saved source and tests. |
| TalkBack | Announce Workspace and each control by name/role; reach editor text and terminal output; perform a harmless edit/undo; return focus predictably after a command. Test both keyboard open and closed. |
| Interrupt and reconnect | Switch apps, return, briefly disconnect, reconnect and confirm saved work. Distinguish a reconnect from an autosaved unsaved buffer. |

Enable VS Code's screen-reader accessibility mode for the editor/terminal check
and record whether that improves the result. A native control's label alone is
not a TalkBack pass. Stop and record blockers rather than assuming a student
could work around them. Include a sustained session before calling the pilot
usable as a student's primary IDE.

The original notebook download and hosted fallback remain available through
their existing workflows; do not edit them as part of this rehearsal.
