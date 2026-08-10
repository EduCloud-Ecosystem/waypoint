# Waypoint — handoff to Ben Markwart, 2026-08-10

*Written by Greg's Cowork session at his request, so Ben has a single,
current, accurate picture of Waypoint's state and the concrete next step —
without having to reconstruct it from `PLAN.1.md`'s ten phases himself.
Sourced directly from `README.md`, `server/NOTES.md`, and `PLAN.1.md` in the
repo as of this date.*

## What Waypoint is

EduCloud's publishing and identity layer: the hosting engine (Coolify), the
identity provider (Keycloak), the tenant onboarding contract, and — a
separate, later workstream — the module registry (catalog + federation
metadata standard). Every other EduCloud module (Cairn, Belay, Outfitter,
Portage) is meant to deploy as a Coolify-conforming container behind this,
with Keycloak as the shared OIDC seam once modules are deployed together.

## Where it actually stands

**Done:**
- **Phase 0** — requirements, tenant intake/onboarding contract, templates.
- **Phase 1.5** — the full local pre-build: demo apps, Dockerfiles, a local
  Coolify-equivalent stack, and the Phase 3 auth round trip (visitor → OIDC →
  app) already proven against a Keycloak realm export, all on Greg's own
  machine. This means the hard integration questions — does the onboarding
  contract actually work, does the auth handoff actually work — are answered
  *before* any money is spent on a server. What's left is standing the same
  thing up on a real host and pointing DNS at it.

**Not done — this is the actual blocker:**
- **Phase 1 — Provision.** This is the only phase that requires a decision
  and a purchase, not more building. Full checklist, verbatim from
  `PLAN.1.md`:
  1. Provision a VM: Ubuntu 24.04 LTS, 4 vCPU, 16 GB RAM recommended (8 GB
     floor), 80 GB disk, SSH key auth, inbound 22/80/443 open.
  2. Verify `sslip.io` wildcard DNS resolves to the VM's IP from a local
     machine (no real domain needed yet — `sslip.io` maps `<ip>.sslip.io`
     automatically).
  3. Confirm SSH access works.
  4. Record provider, region, size, and IP in `server/NOTES.md` and commit.
  - **Checkpoint 1** (the phase's own gate): SSH working + wildcard DNS
    resolving + a human says go. Nothing past this point runs without that.

**Everything after Phase 1 is configuration, not research:**
- Phases 2–9 (install/secure Coolify, deploy the demo apps, wire the real
  Keycloak realm, onboard the first real tenant, launch/operate) all run
  *against* the server once it exists. They follow the pre-built Phase 1.5
  recipe rather than requiring new design work.
- The **registry** (catalog, federation metadata standard) is explicitly a
  separate, proposal-stage workstream — collaborator-led, i.e. yours —
  targeted for the ~December grant season, not part of the Phase 1–9
  sequence above. It doesn't block or get blocked by provisioning.

## What `server/NOTES.md` already has recorded

- **Provider: DigitalOcean** — chosen over Hetzner, which requires
  government-ID verification at signup that Hetzner's own compliance flow
  made a poor fit here.
- **Planned size:** Basic Droplet, 4 vCPU / 8 GB RAM / ~160 GB SSD (the 8 GB
  floor from the Phase 1 checklist, not the 16 GB recommended figure —
  worth deciding whether to size up before purchase).
- **A real cost/reversibility note already written down:** a CPU+RAM-only
  DigitalOcean resize later is reversible; a resize that changes disk size
  is **not** — plan the initial disk size as if it's permanent.
- **Region, IP, and the dashed VM-IP form:** still `TBD` — this is the one
  concrete decision + action nobody has made yet.
- **Single-VM, no failover:** accepted in writing as a settled decision for
  now (SPOF is a known, deliberate tradeoff at this scale).
- **Firewall plan:** 22/80/443 open permanently; port 8000 opened
  temporarily for Coolify's first-run setup only, then closed.
- **Still TBD for later phases (not blockers, just not decided yet):** TLS
  approach, which Coolify version, the dashboard URL, and — for Phase 9 —
  a scoped Coolify API token for the registry, which is to be stored
  **outside the repo** when it's created.

## The concrete ask

Someone needs to actually provision the DigitalOcean droplet per the Phase 1
checklist above (region + final size are the only real open decisions;
everything else is either already decided or is "run through the checklist
and record what happened"), verify SSH and DNS resolve, write the real
values into `server/NOTES.md` in place of `TBD`, and commit. That single
step is Checkpoint 1, and it's what unblocks Phases 2–9 — which, per the
plan itself, are mechanical from there.

Read `PLAN.1.md` for the full Phase 1 text and Phases 2–9 if you want the
whole sequence at once; `server/NOTES.md` is the living record to update as
each TBD gets filled in.
