# Reviewed replacement cutover

This workflow transfers a stopped managed course to a replacement namespace on
**the same native worker**. It preserves the original public origin and identity
client while retaining both copies of saved work. It never starts a Hub, changes
DNS, edits the identity provider or switches a public proxy. The operator reviews
and performs routing/startup after the durable transfer. Cross-worker fencing,
automated disaster failover and hosted acceptance remain separate work.

All worker operator controls must use the current Waypoint release before the
first transfer. Historical allocators refuse the extended ledger, including for
unaffected peers. Running peer services keep their configuration; current controls
verify their reservations and continue to admit them. Historical direct scripts
and root/Docker actions cannot enforce newer policy.

## Prepare a replacement

1. Stop the source Hub and all learner servers. Keep the peer courses running.
2. Take an exact encrypted managed backup with current tools.
3. Restore it into a new private directory, distinct namespace and distinct XFS
   filesystem and a distinct loopback port, using the current roster/credential
   and existing immutable images.
4. Retain the required-allocation gate and private source/recovery history. Ordinary
   enrollment must still refuse the old identity collision.
5. Preview and review a new private cutover plan outside retained course/worker state.

The source and receiver must have exactly the same approved roster, learner RAM,
CPU, active cap, image IDs and quota policy. Plans bind current worker authority,
ledger, environment, setup, endpoint enrollment/recovery history and every retained
home. Removed learners are included. File bytes, modes, uid/gid ownership, membership and literal
symlink targets must match; special entries and unstable inspection refuse. The
backup's snapshot ID alone does not prove that source work is still current.
Inspections are bounded to 200,000 entries, 100 GiB of retained regular-file bytes
and 2 GiB per file. Orphan/missing registered homes or owned Docker home volumes
refuse; they cannot be silently omitted from preservation proof.
Changes to resource limits use the separate [resource workflow](RESOURCE-CHANGES.md)
after cutover.

## Review, apply, resume

```sh
sudo python3 cutover.py preview \
  --source /srv/educloud/course-one --destination /srv/educloud/replacement \
  --plan /secure/cutover-plan.json
sudo python3 cutover.py apply \
  --source /srv/educloud/course-one --destination /srv/educloud/replacement \
  --plan /secure/cutover-plan.json
```

Keep plans and configuration private: they contain complete environments and
credentials. Plans cannot be edited or reused after state changes. Both course
locks are acquired in canonical path order before the worker lock. Pending journals
block startup, backup and policy/lifecycle changes on both participants; stop/status
and unrelated peer operations remain available. Never remove a pending journal.

Apply seals the sending course before atomically transferring its reservation,
then verifies receiver enrollment and complete decision receipts before clearing
receiver and interruption gates. Identical resource profiles let the source retain
the full reservation until the atomic switch without overcommit or a capacity gap.
The original record becomes `TRANSFERRED`, pointing to its successor. Physical
storage, filesystem, directory and namespace collisions remain forbidden; reviewed
identity lineage allows only its own logical identity/port overlap. The current
sender and receiver must use distinct loopback ports; historical lineage port
reuse does not authorize a direct same-port cutover. Ordinary
creation of another colliding course remains refused.

```sh
sudo python3 cutover.py resume \
  --source /srv/educloud/course-one --destination /srv/educloud/replacement \
  --plan /secure/cutover-plan.json
```

Resume verifies durable transaction state under the same locks. A completed retry
is idempotent only while that decision is still current. Generated private
`WORKSPACE_CUTOVER_REVISION` metadata and latest receipts prevent old approval
replay after a forward/rollback cycle or history loss. Do not edit revision metadata,
remove completed history or manually turn a transferred lease into an active one.

After completion, preflight the receiver, review the reverse-proxy target, explicitly
start the receiver with current tools, and verify actual authenticated saved work.
The sending course remains fenced even if its local replacement marker disappears,
because the ledger refuses ordinary transferred-course enrollment and activation.
There is no deletion operation; original homes and history remain retained.

## Reviewed rollback

```sh
sudo python3 cutover.py rollback-preview \
  --source /srv/educloud/replacement --destination /srv/educloud/course-one \
  --plan /secure/rollback-plan.json
sudo python3 cutover.py apply \
  --source /srv/educloud/replacement --destination /srv/educloud/course-one \
  --plan /secure/rollback-plan.json
```

`--source` always names the current sender and `--destination` the next receiver,
so reverse the two paths for rollback. Both participants must again be stopped.
Rollback is an explicit reviewed transfer
of activation back to the immediate original sender, never an automatic response
to a readiness timeout. It refuses any divergence across retained homes, including
replacement-only work and hidden notebook runtime/history changes. Thus it is most
useful before learners begin using the replacement. It never discards or silently
copies newer data. Reconcile divergent work and retain backups before planning a
separate recovery action. After rollback, switch routing explicitly and restart
the accepted sender only after current preflight. A new reviewed forward decision
may reactivate a still-equal immediate receiver; old plans remain stale.

## Recovery and synthetic acceptance

Current backups validate active decision history before provider/export effects
and retain the latest cutover receipt inside encryption. Fresh restoration keeps
source history only, strips the active cutover revision and requires current worker
enrollment. It does not reinstall source fences, approvals or pending journals.

The disposable native `rehearsal/verify_cutover.py` exercises the real origin/IdP,
restored saved work and a running peer, interruption/resume, pre-use rollback,
repeated approvals and safe refusal after receiver work diverges. Its fixture-only
bridge relay routes to the receiver's actual loopback endpoint; it is not a hosted
routing service. Only bounded `CUTOVER-PASS.json` is uploaded after every required
control and owned-resource cleanup. Passing it establishes synthetic component
behavior, not production cutover or cross-host failover.
