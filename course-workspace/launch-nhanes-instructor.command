#!/bin/zsh
# Local synthetic instructor workflow; never a student deployment.
set -eu
umask 077
cd -- "${0:A:h:h}"
mode="${1:---live}"
if [[ "$mode" != --check && "$mode" != --stub && "$mode" != --verify-stub && "$mode" != --live ]]; then
  print -u2 'Use --check, --stub, --verify-stub, or --live.'; exit 2
fi
driver=/Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python
kernel=/Users/gregchism/.codex/worktrees/waypoint-nhanes-qualification/waypoint/output/nhanes-qualification-v1/kernel-env/bin/python
cairn=/Users/gregchism/.codex/worktrees/cairn-nhanes-handoff/cairn
belay=/Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1
portage=/Users/gregchism/.codex/worktrees/classroom-live-tutoring/portage
keyfile=/Users/gregchism/dev/.educloud-worktrees/portage-system-one-shadow-pilot-v1/.qualification/openrouter/.env
[[ -x "$driver" && -x "$kernel" && -d "$cairn" && -d "$belay" && -d "$portage" ]] || { print -u2 'A qualified local checkout or environment is missing. See NHANES-LIVE-TESTING.md.'; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { print -u2 'Commit the Waypoint source before starting a qualified window.'; exit 1; }
cairn_pin=46c963b59feb67324714e308e8243c779cb14a81
if [[ "$mode" == --live || "$mode" == --check ]]; then
  cairn_pin=2eb0a0415f791f7d53734d5909a3a3d7454e34f0
fi
[[ "$(git -C "$cairn" rev-parse HEAD)" == "$cairn_pin" && "$(git -C "$belay" rev-parse HEAD)" == c5a27073e74b9eeb7ab697190915378b50132883 && "$(git -C "$portage" rev-parse HEAD)" == b72881f009e61e1311b6e46b92b42841991fd163 ]] || { print -u2 'The component pins for this mode do not match. The revised UI is eligible for --stub and --verify-stub.'; exit 1; }
if [[ "$mode" == --live || "$mode" == --check ]]; then
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
  --checkout-image sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c \
  --output "$out" --execute-synthetic "${args[@]}"
