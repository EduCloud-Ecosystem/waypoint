#!/bin/zsh
# Local synthetic instructor workflow; never a student deployment.
set -eu
umask 077
export PATH="/Users/gregchism/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.26.11.darwin-arm64/bin:$PATH"
export GOTOOLCHAIN=local
cd -- "${0:A:h:h}"
mode="${1:---live}"
if [[ "$mode" != --check && "$mode" != --stub && "$mode" != --verify-stub && "$mode" != --learner && "$mode" != --sign-in && "$mode" != --sign-in-hints && "$mode" != --verify-learner-hints && "$mode" != --verify-learner && "$mode" != --verify-live && "$mode" != --live ]]; then
  print -u2 'Use --check, --stub, --verify-stub, --learner, --sign-in, --sign-in-hints, --verify-learner, --verify-learner-hints, --verify-live, or --live.'; exit 2
fi
driver=/Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python
kernel=/Users/gregchism/.codex/worktrees/waypoint-nhanes-qualification/waypoint/output/nhanes-qualification-v1/kernel-env/bin/python
cairn=/Users/gregchism/dev/.educloud-worktrees/cairn-studio-classic-v1
belay=/Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1
portage=/Users/gregchism/.codex/worktrees/portage-openbao-trial/portage
keyfile=/Users/gregchism/dev/.educloud-worktrees/portage-system-one-shadow-pilot-v1/.qualification/openrouter/.env
belay_pin=c5a27073e74b9eeb7ab697190915378b50132883
cairn_pin=533c16a414b9fef1979d0de783b3436f31dd03a7
if [[ "$mode" == --live || "$mode" == --verify-live || "$mode" == --check ]]; then
  belay=/Users/gregchism/dev/.educloud-worktrees/belay-nhanes-source-check
  belay_pin=0b960dffb779fd5745b45f351fe9c7145c88d055
  cairn="${NHANES_LIVE_CAIRN_CHECKOUT:-$cairn}"
  cairn_pin=533c16a414b9fef1979d0de783b3436f31dd03a7
fi
[[ -x "$driver" && -x "$kernel" && -d "$cairn" && -d "$belay" ]] || { print -u2 'A qualified local checkout or environment is missing. See NHANES-LIVE-TESTING.md.'; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { print -u2 'Commit the Waypoint source before starting a qualified window.'; exit 1; }
[[ "$(git -C "$cairn" rev-parse HEAD)" == "$cairn_pin" && "$(git -C "$belay" rev-parse HEAD)" == "$belay_pin" ]] || { print -u2 'The component pins for this mode do not match. Restore the reviewed source revisions before retrying.'; exit 1; }
if [[ "$mode" == --live || "$mode" == --verify-live || "$mode" == --check ]]; then
  [[ -d "$portage" && "$(git -C "$portage" rev-parse HEAD)" == 4f7ac0d3e839012664d4b8272c8446376efc8f1b ]] || { print -u2 'The live Portage checkout must match the reviewed revision.'; exit 1; }
  [[ -f "$keyfile" && "$(stat -f %Lp "$keyfile")" == 600 ]] || { print -u2 'The existing private provider key file is missing or has incorrect permissions.'; exit 1; }
fi
if [[ "$mode" == --check ]]; then
  print 'Local source pins, environment paths and key-file permissions checked. No services or model requests started.'; exit 0
fi
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="output/nhanes-instructor-$stamp-$$"
mkdir -p output
args=(--tutor-mode stub --operator-review-seconds 120)
if [[ "$mode" == --live ]]; then
  args=(--tutor-mode live --live-portage "$portage" --env-file "$keyfile" --execute-approved --live-review-seconds 240)
elif [[ "$mode" == --verify-live ]]; then
  args=(--tutor-mode live --live-portage "$portage" --env-file "$keyfile" --execute-approved)
  print 'Verifying one live hint with the supplied question. Maximum 7 requests and $0.175 reserved.'
elif [[ "$mode" == --sign-in-hints ]]; then
  args=(--manual-sign-in --learner-review-seconds "${2:-540}" --tutor-mode stub)
elif [[ "$mode" == --verify-learner-hints ]]; then
  args=(--verify-learner-starter --tutor-mode stub)
elif [[ "$mode" == --sign-in ]]; then
  args=(--manual-sign-in --learner-review-seconds "${2:-540}")
elif [[ "$mode" == --learner ]]; then
  args=(--learner-review-seconds "${2:-540}")
elif [[ "$mode" == --verify-learner ]]; then
  args=(--verify-learner-starter)
elif [[ "$mode" == --verify-stub ]]; then
  args=(--tutor-mode stub --verify-studio-ui)
fi
print "Preparing the synthetic NHANES browser session. Evidence: $PWD/$out"
if [[ "$mode" == --live ]]; then
  print 'Live mode permits only the supplied question, at most 7 requests and $0.175 reserved. No paid hint is requested until you click Ask for a hint.'
fi
print 'Keep this terminal open. Press Ctrl-C to stop. The workspace and tutor have separate time limits.'
"$driver" course-workspace/rehearsal/nhanes_desktop.py \
  --cairn "$cairn" --belay "$belay" --kernel-python "$kernel" \
  --kernel-lock course-workspace/rehearsal/nhanes-kernel-requirements.lock \
  --grader-image sha256:d8e7745e5644f4e4c4dd49902981a8953485cc188a2263d866ba042aa0e4fab7 \
  --checkout-image sha256:6c6e6991e810c2ef88147fb47e9d98493095b8d2ad17e51e22f157fd87aae065 \
  --output "$out" --execute-synthetic "${args[@]}"
