# Waypoint notebook accessibility candidate

This optional adapter is for the seven-cell, public-data NHANES rehearsal on
JupyterLite 0.8.7 and JupyterLab 4.6.5. The hosted course workspace remains the
fallback. Nothing here changes that service, the assignment archive, submission,
or the published prerelease.

## Prepare a test site

1. Stage the exact [cold-run inputs](../docs/evidence/2026-11-13-nhanes-browser-runtime.md#cold-loopback-run-and-device-checks)
   in an ignored directory. Build from the reviewed starter, with the Pyodide
   entry point and wheel index served locally and PyPI fallback disabled. Use
   the course operator's approved HTTPS fallback in the source bundle. A
   reserved `.invalid` origin produces a notice with no link.
2. Run `python3 browser-candidate/prepare.py /absolute/staged-site /absolute/new-candidate`
   from the checkout. Serve the new directory, and open
   `/lab/index.html?path=nhanes-wrangling.ipynb`. Keep this build isolated from
   the hosted workspace and other JupyterLite storage origins. Use loopback
   for desktop tests; see the device runbook for HTTPS on a phone or tablet.
3. Use only the reviewed public CSVs and synthetic reference work. Keep the
   reference solution outside the served site. Download your work separately
   before closing the browser. The buttons neither submit nor grade it.

Preparation refuses a different runtime version, unreviewed starter files, or
an existing destination. Use a trusted operator-built site as input; this tool
does not verify every runtime file or sanitize a foreign site. It copies the
site rather than modifying it. To roll back, stop serving the candidate and
retain separately downloaded work. There is no production deploy step.

The adapter uses the pinned Lab shell and command API, with local markup repairs
for known elements in this build. It deliberately does not change notebook
sources, kernel execution, or the trusted-file catalog. Settings enable
CodeMirror line wrapping and keep the small notebook's cells mounted for
consistent reading order. On narrow screens the file panel starts closed; the
named Show/Hide files control keeps it available. Notebook actions and each
individual download have native buttons, visible focus, and status text.

Upstream version changes require rechecking selectors, labels, focus behavior,
and byte-for-byte export. The adapter is not intended for untrusted extensions,
arbitrary notebooks, or general JupyterLab deployments. Do not treat passing
automated scans as accessibility conformance.
