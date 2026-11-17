# Recorded OpenBao adoption invocation

This is the exact successful 2026-11-16 local invocation, not a standing paid-call
authorization. It requires the application pins in OPENBAO-PILOT-ADOPTION.md, clean
tracked checkouts, the recorded environments and private operator credential path.
Current worktrees may contain documentation-only descendants; do not silently
substitute them for the frozen pin. A separate rerun requires a fresh output path
and fresh owner-only custody directory outside Git/runtime. Do not overwrite the
successful run or its recovery package. No credential values are included here.

```sh
#!/bin/sh
# Recorded local verification command; running it consumes a new finite allowance.
set -eu
cd /Users/gregchism/.codex/worktrees/portage-openbao-trial/portage
docker load -i /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial/output/openbao-trial/checkout-image.tar > .qualification/adoption-live-image-restore.log
docker pull codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe >> .qualification/adoption-live-image-restore.log
PATH="/Users/gregchism/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.26.11.darwin-arm64/bin:$PATH" GOTOOLCHAIN=local uv run --frozen python integrations/ai_pilot/openbao_classroom.py \
 --bao .qualification/openbao-bin/bao \
 --provider-key-file /Users/gregchism/dev/.educloud-worktrees/portage-system-one-shadow-pilot-v1/.qualification/openrouter/.env \
 --waypoint /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial \
 --waypoint-pin 3c8232c405b5190dc554bea31f9d631accb077fd \
 --driver /Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python \
 --cairn /Users/gregchism/dev/.educloud-worktrees/cairn-studio-classic-v1 \
 --belay /Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1 \
 --kernel-python /Users/gregchism/.codex/worktrees/waypoint-nhanes-qualification/waypoint/output/nhanes-qualification-v1/kernel-env/bin/python \
 --kernel-lock /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial/course-workspace/rehearsal/nhanes-kernel-requirements.lock \
 --grader-image sha256:d8e7745e5644f4e4c4dd49902981a8953485cc188a2263d866ba042aa0e4fab7 \
 --checkout-image sha256:6c6e6991e810c2ef88147fb47e9d98493095b8d2ad17e51e22f157fd87aae065 \
 --output .qualification/openbao-classroom-adoption-01 \
 --custody-export '/Users/gregchism/Library/Application Support/EduCloud/OpenBao Recovery/classroom-adoption-01/recovery.json' \
 --execute-approved > .qualification/openbao-classroom-adoption-01.log 2>&1
```

Observed: all eight live workflow controls passed; three model transports,
zero SDK retries, $0.0035944 provider-reported cost. Post-workflow recovery used
no additional model calls and preserved the actual ledger. Expected parent exit
is zero, failure lists empty, cleanup true, secret scan passed. Institutional
password-manager import and separate backup remain unconfirmed; this local
rehearsal package is not the custody record for any future hosted deployment.
