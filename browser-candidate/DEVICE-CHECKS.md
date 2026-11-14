# Physical device and assistive technology checks

Status: **prepared, not run on physical devices or with a screen reader**.
Keep the hosted fallback throughout. Use only the reviewed public NHANES
starter and synthetic reference solution. Do not record learner identities,
private files, authentication secrets, or assistive technology recordings
without participant consent.

## Setup and evidence

- Use the exact candidate revision and runtime file hashes from the rehearsal
  record. Prepare an isolated test origin with HTTPS trusted by the device.
  A phone's loopback points to the phone itself. Use a reachable, authorized
  test server; do not expose a developer's local server publicly. Use an
  operator-approved hosted workspace as the fallback and verify the URL.
- Start with one actual low-end Android phone using Chrome and one iPhone or
  iPad using Safari. Record hardware, OS and browser versions, orientation,
  text zoom, network conditions, candidate revision, and result per step.
  Test normal and constrained network separately, with a fresh site profile.
  Deny external traffic while keeping the candidate origin reachable. Preserve
  browser and server request evidence; distinguish attempted browser background
  traffic from notebook traffic.
- Use a dedicated test browser profile or origin. Save independent copies before
  clearing **only that test origin's** storage. Record any failed step before
  attempting a workaround. Use the host's clock or video timestamps to record
  first usable controls, first result, and export completion.

## Touch only

1. Open the notebook in portrait without a hardware keyboard or mouse. Check
   that the file panel is closed, the main controls remain tappable, and
   wrapping keeps code readable. Repeat with large text and in landscape.
2. Tap Show files and then Hide files. Open the notebook and select Python
   (Pyodide). With the on-screen keyboard, enter a small edit in the synthetic
   solution cell and undo it. Check that focus, cursor position, and the soft
   keyboard do not hide the cell or prevent returning to the controls.
3. Tap Run all cells. Confirm that the actual result matches the expected
   192 rows and [recorded CSV checksum](REHEARSAL.json) for the synthetic fixture. Tap Save notebook, then
   Download files. Use the four named buttons individually. Confirm the
   downloads are usable from the device's file manager and compare the CSV
   digest on a trusted machine. Do not count a browser download notification
   as evidence of correct bytes.
4. Close the tab and reopen. Then simulate lost browser storage in the
   disposable profile. Recover from the downloaded files in a clean profile.
   Open and save the uploaded notebook before redownloading, then compare all
   four byte hashes against the original downloads. If the notebook is
   reserialized, also compare its full parsed JSON (cells, outputs and metadata),
   record the byte mismatch, and keep the original untouched. The hosted
   fallback must remain reachable if recovery,
   editing, or download fails.

## Keyboard and screen reader

- With NVDA and Chrome or Firefox on Windows, test the keyboard workflow
  without a mouse. On the physical iOS/iPadOS device use VoiceOver with Safari;
  on Android use TalkBack with Chrome. Record exact combinations tested.
- Verify reading order and names for notebook actions, the file toggle and its
  expanded state, the fallback link, cells, code editors, and scrollable output.
  Open the notebook with the Go to notebook control. Reach the solution cell,
  enter and leave edit mode, traverse wrapped lines, and return to the actions.
  Check visible focus and no trap. Test JupyterLab's own toolbar buttons for
  duplicate or missing announcements (the automated scan requested manual
  review of their host attributes).
- Listen for Run and Save status, then open the download dialog. Confirm the
  dialog name, focus placement, all four button names, Escape/Close, and focus
  return. Confirm modal focus does not reach the underlying notebook. Exercise
  empty/missing download and kernel error feedback separately; the Run status
  is not proof of successful execution.
- Use assistive navigation to read the rendered table and scroll outputs.
  Repeat at 200% desktop zoom and at the user's preferred text size on phones.
  Preserve blocker details, expected/actual behavior and a small redacted
  screenshot or transcript when appropriate.

A run passes only when the operator records correct outputs and independent
recovery, usable touch and keyboard workflows, comprehensible announcements,
and a working authorized fallback. Record each device/assistive technology
combination separately. A failure or unrun combination does not inherit an
emulator or axe result. Course submission, hosted access, and capacity have
separate acceptance gates.
