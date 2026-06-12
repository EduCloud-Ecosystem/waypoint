# NOTES.md: server facts for the EduCloud hosting layer dev VM

No secrets in this file, ever (no passwords, tokens, or keys). Conventions: LF
endings, no em dashes or en dashes.

## Provider and VM

- Provider: DigitalOcean (switched from Hetzner before any server was created;
  Hetzner required government ID verification at signup, declined; that account
  is being deleted). See CHANGELOG Release 5.
- Region: TBD (chosen at creation; any region is fine, sslip.io is region-agnostic)
- Plan: Basic Droplet, 4 vCPU / 8 GB RAM / ~160 GB SSD, resizable. Confirm exact
  disk/transfer in the DO console at creation.
- Resize remedy (documented): if Phase 4 R builds/containers show memory
  pressure, resize up to the 8 vCPU / 16 GB Droplet. On DigitalOcean a
  CPU-and-RAM-only resize is REVERSIBLE; a resize that also grows the disk is
  PERMANENT (disk cannot shrink later). 8 GB is the plan floor per
  REQUIREMENTS.md.
- OS: Ubuntu 24.04 LTS
- Public IPv4: TBD
- VM-IP-dashed: TBD (IPv4 with dots replaced by dashes, for sslip.io)
- SSH user: root (DigitalOcean default), key auth only
- Backups: DigitalOcean backups ENABLED (provider snapshots; dev backup approach
  per REQUIREMENTS.md section 4)

## Topology and SPOF

One VM for dev. Single point of failure accepted in writing (settled decision 2,
INTEGRATION.md). Control-plane / workload split deferred to promotion
(docs/PROMOTION.md).

## Placeholder domains (DEV MODE, sslip.io)

Derived from the public IPv4 once known. With VM-IP-dashed = a-b-c-d:

- HOSTNAME (platform):   apps.a-b-c-d.sslip.io
- Coolify dashboard:     coolify.apps.a-b-c-d.sslip.io
- Keycloak:              auth.apps.a-b-c-d.sslip.io
- Per app:               <app>.apps.a-b-c-d.sslip.io

## Firewall (inbound)

- 22/tcp  SSH
- 80/tcp  HTTP (Traefik)
- 443/tcp HTTPS (Traefik)
- 8000/tcp TEMPORARY, Phase 2 Coolify first-run only; restrict to your IP and
  close after the dashboard domain is set. Not left open.

Implemented at creation: 22 restricted to home IP, 80 and 443 open, 8000 closed,
DigitalOcean Cloud Firewall applied to the Droplet.

## Scheme in use (TLS)

- TBD in Phase 2 (HTTPS if Let's Encrypt issues for sslip.io names, otherwise
  HTTP; HTTP is acceptable in dev and not a checkpoint failure).

## Coolify

- Version: TBD (Phase 2)
- Dashboard URL: TBD (Phase 2)

## Registry seam

- Scoped Coolify API token: TBD (Phase 9; stored outside the repo, location
  noted here, value never in repo).
