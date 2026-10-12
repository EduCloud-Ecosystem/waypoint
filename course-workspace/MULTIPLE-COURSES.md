# Multiple courses on one worker

`allocation.py` reserves maximum learner/Hub RAM and CPU for each enrolled course
under one native Docker worker policy. It checks aggregate commitments, identities
and storage before activation. Stopped courses retain their reservation; uncertain
starts never silently free capacity. This is operator-controlled admission, not a
queue, autoscaler, hypervisor boundary or measurement of class capacity.

## Review a worker policy

Use the current checkout as a worker administrator on native Linux with the
verified `/var/run/docker.sock` daemon. Native Linux trial startup also requires
administrator/root for the shared lock. Private Desktop trials retain the user
entry point before managed authority and refuse another live course.

Create a root-owned mode-0600 policy outside deployments, homes and its state
directory. Use canonical paths without symlink ancestors. This example reserves
4 GiB/one CPU for host services and allows at most 8 GiB/three CPUs of course
commitments; the actual worker must have at least 12 GiB/four CPUs. Choose limits
from institutional measurements. These are resource limits, not monetary budgets.

```json
{
  "version": 1,
  "worker_id": "course-worker-one",
  "memory_budget_mb": 8192,
  "cpu_budget": 3,
  "host_reserve_mb": 4096,
  "host_reserve_cpu": 1,
  "state_directory": "/var/lib/educloud/course-allocations",
  "owner": "course-support"
}
```

All fields are required. Initialization creates private state under an existing
root-controlled parent; keep the policy outside it. One fixed
`/var/lib/educloud/worker-allocation` authority binds policy, location and ledger
to the actual daemon ID. The persistent sibling `.worker-allocation.lock` prevents
another policy/state directory reserving the same daemon again. Do not replace
lock inodes, erase authority or edit claims to conceal drift.

Each claim charges the actual Hub limits (1,024 MiB/one CPU), plus
`active_limit * memory_mb` and `active_limit * cpu` for learners. Host reserve is
charged once. ACTIVE claims count even without running notebooks. Budgets plus
reserve must fit actual Docker RAM/CPU; claims must fit both budgets. Other services
need reviewed reserve/monitoring; arbitrary Docker workloads are not metered.

## Enroll before startup

Prepare each [course manifest](COURSE-MANIFEST.md), images, identity client and
quota-backed homes. Stop the course and all storage writers before enrollment.
Use distinct instances, loopback ports, nonoverlapping dedicated DNS subtrees,
home namespaces and issuer/client pairs. The same learner subject may belong to
both courses; files, sessions and authorization remain separate.

Use one dedicated quota-enabled XFS filesystem per managed course. Enrollment
reads its actual filesystem UUID and freezes it in the claim; another root on
the same filesystem, or a cloned filesystem with the same UUID, refuses. Shared
filesystem quota/project allocation is not supported by this version. A changed
filesystem identity needs investigation rather than automatic claim adoption.
Mount inspection uses explicit JSON columns and compares the actual target,
following the [util-linux findmnt manual](https://github.com/util-linux/util-linux/blob/master/misc-utils/findmnt.8.adoc).

```sh
sudo python3 allocation.py enroll --directory /srv/educloud/course-one \
  --config /etc/educloud/worker-policy.json
sudo python3 allocation.py enroll --directory /srv/educloud/course-two \
  --config /etc/educloud/worker-policy.json
sudo python3 deployment.py start --directory /srv/educloud/course-one
sudo python3 deployment.py start --directory /srv/educloud/course-two
sudo python3 allocation.py inventory --config /etc/educloud/worker-policy.json
```

The sticky `.worker-allocation.json` is PENDING before the durable claim, then
ENROLLED. Retry the exact enrollment after interruption; changed policy/claim
refuses. Missing/corrupt authority, ledger or marker cannot become an unmanaged
start. Once authority exists, all current startup paths require institutional
enrollment even with every managed course stopped. Before authority exists,
legacy startup permits one live course and uses the same native lock.

Public activation holds the course lock and checks under the worker lock before
Compose consumes configuration. CLI startup holds admission through readiness.
Timeout/failure retains its lease. Reports expose totals/counts/digests, not full
claims, paths, subjects, owner or credentials. Inventory does not automatically
free processes or leases. Existing roster/quota maintenance and image upgrades
remain available. Frozen resource/identity/origin/port/storage claims cannot be
amended in place; resource amendment is a separate follow-up workflow.

## Release capacity, preserve coursework

Stop writers before explicitly releasing a course. For closure, complete
[retirement/export](COURSE-RETIREMENT.md) before release.

```sh
sudo python3 deployment.py stop --directory /srv/educloud/course-one
sudo python3 allocation.py release --directory /srv/educloud/course-one
```

Release proves Hub/learner and foreign home/Hub volume writers stopped. It retains
files, volumes, configuration and identity/storage collision records. Released
courses cannot start. Re-enroll the exact original stopped claim to request
capacity again; changed claims/insufficient resources refuse. Retirement continues
to block activation. Strict released tombstones prevent a fresh namespace reusing
the old identity/storage through ordinary enrollment. This version has no transfer,
record-deletion or automatic-expiry command.

## Recover with current allocation

Managed backups use version 4, an explicit `retirement_protected` boolean, private
original binding and the allocation runtime. Version-1/2/3-only restorers refuse
them; ordinary historical packages remain supported. Restore writes
`.worker-allocation-required.json` before usable environment/home allocation.
The prior binding is private `source-worker-allocation.json` history. Source
worker authority, policy and shared leases are never installed on the destination.

On an institution-approved replacement worker, review a current policy and enroll
the restored nonretired course before starting. The gate clears only after durable
enrollment verification. A retired backup stays closed and supports coursework
export; it cannot be enrolled for activation. Same-worker live replacement needs
a future reviewed identity transfer because of retained tombstones.

Guards apply to current entry points and managed version-4 recovery. Historical
unmanaged scripts and direct root/Docker operations cannot enforce newer authority.
Use current controls and institutional operator restrictions. Shared-kernel trust,
identity/TLS, off-host recovery, load, monitoring and support remain actual-host gates.

## Disposable acceptance

Run `rehearsal/verify_multiple_courses.py --output /tmp/NEW_PRIVATE_DIRECTORY` as
root on disposable Linux. Two authenticated courses use the same synthetic subjects,
bounded fixture services, separate homes and actual concurrent Python/R. Controls
cover cookie replay, file isolation, admission saturation, revocation, retirement,
release and protected closed restoration while the second course retains its
server/work. This proves closed recovery, not reopening a replacement namespace.

Only bounded `MULTI-COURSE-PASS.json` is uploaded after all controls and verified
cleanup. Policies, claims, authority, environments, identities, keys and plaintext
exports stay private. The harness disposes only its exact synthetic authority
after proving owned resources absent, preserving the global lock inode. That
disposal helper is not a production deletion API.
