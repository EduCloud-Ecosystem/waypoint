# Recorded successful live invocation

This records the 2026-11-16 execution, not an automatic rerun or spending grant.
All paths name isolated trial or existing pinned read-only dependencies. Use a new
output path for any separately authorized repeat. The successful run used the
Waypoint application revision below; later documentation-only commits are not
silently substituted for this pin. No credential values appear here.

Run from `/Users/gregchism/.codex/worktrees/portage-openbao-trial/portage` at
`2ac908615a6d306f7862622cf5fd8ff1eed0c1a1`. Restore/check images immediately before
the evaluator in the same shell; do not prune shared Docker resources.

```sh
docker load -i /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial/output/openbao-trial/checkout-image.tar
docker pull codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe
PATH="/Users/gregchism/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.26.11.darwin-arm64/bin:$PATH" GOTOOLCHAIN=local uv run python integrations/ai_pilot/openbao_classroom.py \
  --bao .qualification/openbao-bin/bao \
  --provider-key-file /Users/gregchism/dev/.educloud-worktrees/portage-system-one-shadow-pilot-v1/.qualification/openrouter/.env \
  --waypoint /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial \
  --waypoint-pin 6d44170806bbf7a1fba846fb341d8dfd9d737f3a \
  --driver /Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python \
  --cairn /Users/gregchism/dev/.educloud-worktrees/cairn-studio-classic-v1 \
  --belay /Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1 \
  --kernel-python /Users/gregchism/.codex/worktrees/waypoint-nhanes-qualification/waypoint/output/nhanes-qualification-v1/kernel-env/bin/python \
  --kernel-lock /Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial/course-workspace/rehearsal/nhanes-kernel-requirements.lock \
  --grader-image sha256:d8e7745e5644f4e4c4dd49902981a8953485cc188a2263d866ba042aa0e4fab7 \
  --checkout-image sha256:6c6e6991e810c2ef88147fb47e9d98493095b8d2ad17e51e22f157fd87aae065 \
  --output .qualification/openbao-classroom-live-01 \
  --execute-approved
```

Expected success: exit zero, empty failure lists, both workflow and OpenBao cleanup
true, secret-leak scan passed, credential reads equal actual transports. Observed:
three transports, zero SDK retries, $0.0035964 provider-reported cost. Provider-internal
retry count is unavailable. Ordinary kernel work and deterministic checks must pass;
a receipt alone is not grading or instructional acceptance.

The private raw bundle retains audit records, earlier failures, browser screenshots,
provider accounting and encrypted disposable storage. Do not publish raw artifacts
or expose the private operator key file. The adjacent verification JSON is an
allowlisted summary plus environment identities and hashes.
