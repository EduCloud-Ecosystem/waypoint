# Submit saved Jupyter files to Cairn

The course image contains a small authenticated Jupyter Server extension and a
JupyterLab launcher plugin. When an operator configures `WORKSPACE_CAIRN_ORIGIN`,
JupyterLab shows **Submit saved files to Cairn** in the top bar and the Coursework
launcher category. The default workspace remains `/lab`. Without that setting,
the bridge endpoints and links are unavailable.

## Operator setup

Set the credential-free Cairn origin in the deployment's private environment:

```dotenv
WORKSPACE_CAIRN_ORIGIN=https://cairn.example.edu
```

Rebuild the course image, deploy the matching Hub configuration, and restart
learner servers to apply the environment. Register the workspace URL on the
corresponding Cairn assignment. Cairn independently checks the selected
assignment's allowed workspace origin before receiving files. Do not add
Cairn credentials or authentication tokens to this setting.

Institutional deployments require HTTPS. `WORKSPACE_CAIRN_LOCAL_TEST=1` permits
only HTTP loopback Cairn origins (`127.0.0.1`, `localhost`, or `::1`) for synthetic
fixtures; `local-test` Hub mode sets this automatically. An institutional TLS
fixture may opt in explicitly to reach a synthetic loopback Cairn receiver.
This flag does not relax the workspace's own institutional HTTPS requirement.
Never enable it for a real learner deployment.

The deployment keeps isolated notebook networks and does not add server egress.
The **learner's browser** reaches both authenticated services separately.
Credentials remain in their own origin; no proxy, token exchange, webhook,
background transfer, or server-side request to Cairn is added.

## Learner workflow

1. Save notebooks and files in JupyterLab, then choose **Submit saved files to
   Cairn**. Files must be within the server's contents root.
2. Browse folders and explicitly select saved regular files. Hidden files,
   symlinks, unsafe or non-ASCII paths, and paths over 240 characters are
   excluded or rejected. Each name starts with a letter or digit and may then
   contain letters, digits, underscores, hyphens, or single separating dots;
   spaces, trailing dots, and consecutive dots are unsupported. The limit is
   32 files, 1 MiB per file, 4 MiB total.
3. Choose **Prepare saved files**. This reads the saved bytes and displays each
   filename, size, and SHA-256 hash. It preserves notebook outputs and all other
   saved content; it does not scrub personal information or serialize unsaved
   editor buffers. Review the content before preparing it.
4. Choose **Continue in Cairn**, allow the popup, sign in if needed, choose the
   assignment, and explicitly receive the files. Review and submit in Cairn.
   Receipt of files by the popup is **not** a submission receipt or grade.

If files change during the server read, preparation fails. Files saved after
preparation do not change the snapshot; prepare again to use the new version.
Clear selection cancels an active handoff. A closed popup or ten-minute timeout
allows a fresh attempt with a new nonce. After a successful handoff, its browser
buffer is discarded. Clearing a handoff cannot recall bytes already received
by Cairn; remove them in Cairn if needed.

## Browser protocol and limits

The authenticated server routes are relative to the user's server base URL:
`cairn/` (page), `cairn/config`, `cairn/list?directory=...`, and `cairn/files`
(POST `{"paths":[...]}`). Jupyter's existing authentication and XSRF checks apply.
No cross-origin API access is enabled. Files are opened relative to a held root
directory descriptor using `O_NOFOLLOW` for every component; special files are
rejected. Folder browsing is bounded to 500 entries.

The opener creates a cryptographically random 64-hex nonce and opens
`CAIRN_ORIGIN/me/submit#waypoint=` followed by URL-encoded JSON
`{version:1,origin:WORKSPACE_ORIGIN,nonce}`. The fragment contains no credentials,
file paths, or file bytes. The receiver validates the source against the selected
assignment and sends `{type:"cairn-ready",version:1,nonce}` after learner consent.
The opener checks exact popup window identity, exact configured Cairn origin,
nonce, version, and phase, then sends once:

```json
{"type":"waypoint-files","version":1,"nonce":"64-hex nonce","files":[
  {"path":"analysis.py","size":123,"sha256":"64-hex digest","content_base64":"..."}
]}
```

Cairn returns `{type:"cairn-received",version:1,nonce}` after validation. Both
sides use exact `postMessage` target origins, never `*`. Invalid or stale messages
are ignored. Browser policies that sever `window.opener` prevent handoff and
require an operator configuration check; they are not bypassed.

The launcher is a small prebuilt module-federation container for the pinned
JupyterLab 4 image. It uses the host's launcher token and Lumino widget instead
of bundling a second framework. Re-run its real-browser integration whenever
updating JupyterLab, its pinned image, or the launcher.

## Verification

```sh
python -m unittest discover -s course-workspace/tests -p 'test_submission_bridge.py' -v
# Requires pinned JupyterLab, Playwright, and its Chromium browser in this venv:
python -m unittest discover -s course-workspace/tests -p 'test_submission_bridge_server.py' -v
```

The first suite checks raw bytes, hashes, size/count/path rules, symlink and FIFO
rejection, bounded browsing, changed files, and environment validation. The
optional integration starts only local synthetic Jupyter and receiver services,
checks login, visible Lab navigation, XSRF rejection, wrong-nonce/origin/window
rejection, popup retry, and exact saved-snapshot transfer. It writes synthetic screenshots under
`output/playwright/`. The full course rehearsal separately tests the Docker
image and actual Cairn receiver. Local synthetic passes are not institutional
production acceptance.

Jupyter's supported server extension and authenticated handler interface is
documented at <https://jupyter-server.readthedocs.io/en/stable/developers/extensions.html>.
