# Studio Classic local instructor test

Status: actual local synthetic NHANES workflow passed 13 integration controls, with zero provider calls and owned-resource cleanup. Seven supplemental browser controls passed against Cairn's opt-in fixture. See `studio-classic-verification-v2.json` (v1 retained as history) for exact application pins, retained failures and evidence hashes. This is not a student deployment or human accessibility acceptance.

Run from this Waypoint worktree:

```sh
course-workspace/launch-nhanes-instructor.command --verify-stub
course-workspace/launch-nhanes-instructor.command --stub
```

The first command is automated and provider-disabled. The second opens a finite synthetic instructor review after the controls. `--live` uses the existing private provider configuration and opens the bounded prepared live question for explicit human action; it is not exercised by this verification. Original accounting and expiry limits remain enforced. No credential belongs in source or screenshots.

The notebook, tutor and saved-file picker use Light/Dark/System. Cairn receives only the appearance enum across its handoff; no course content is stored with that preference. The actual notebook theme follows the choice. Student work, submission confirmation, automated checks and instructor review remain separate.

Environment: the launcher pins cached Python driver/kernel paths, dependency lock, grader/checkout image digests, Cairn and Belay source commits, and cached Go 1.26.11. Cairn's existing `web/package-lock.json` must be installed or matched by the cached dependencies: the accessibility evaluator uses its pinned axe-core. No dependency installation occurred in this run. This launcher is machine-specific; it is not a portable deployment installer.

Supplemental recovery/theme checks use the existing provider-disabled `TestAssessmentBrowserFixture` with `CAIRN_ASSESSMENT_BROWSER_DIR` set to a new private output directory and the provider key option unset. Run `studio_controls.py --fixture <directory>` using the qualified Playwright driver after the fixture writes `url`; the script writes its result and `stop`. It injects one 503 for the results detail request, then verifies the actual response and keyboard recovery, stable history on refresh, three student modes and instructor reflow. The first screenshot pass captured an entry animation; the retained second pass uses reduced motion and waits for content.

Application tests also passed: 764 Waypoint unit tests, Cairn API tests, dashboard build and 25 dashboard tests. The two full-journey failures are retained: early Back navigation before checkpoint handlers (fixed), and a missing cached axe-core dependency in the isolated checkout (restored with the identical lock). Neither incurred provider calls. New worktrees were created with local Git after two app-managed creation attempts stalled without creating a checkout; original worktrees and audit branches were preserved.

The November 15 advisory-review pass adds injected API controls for refreshed assignment metadata, changed grading history while open/closed/in flight, concurrent assignment retries and one-time theme handoff consumption. These validate UI behavior, not actual injected grades. The new full integrated run independently exercises actual grading and receipts. Its initial observer read an incomplete Jupyter disk write and failed; v2 retains that failure. The corrected observer keeps the same 15-second deadline, counts incomplete reads, and requires complete executed authored cells.
