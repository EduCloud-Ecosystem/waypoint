# Maintaining waypoint

Read CLAUDE.md and component contribution guidance before editing.

## Shared classroom pilot protocol

Follow [the EduCloud pilot protocol](docs/development/PILOT_PROTOCOL_V1.md) alongside stricter component guidance and path licenses. It covers ownership, scoped implementation, independent tests, recovery, evidence and bounded agent maintenance. The local digest and actual workflow inventory are in `.github/educloud/component.json`; run `python3 .github/educloud/verify.py`.

Use [EduCloud's integration guide](https://github.com/EduCloud-Ecosystem/educloud/blob/main/docs/pilot/INTEGRATION.md) and [central status](https://github.com/EduCloud-Ecosystem/educloud#pilot-status) for cross-component context. Keep local tests, CI, merge, deployment and real-user acceptance distinct. Automated reviewers cannot authorize spending, merge, cloud resources or student admission. Preserve other owners' work and retained failures.
