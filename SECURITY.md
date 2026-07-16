# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately rather than opening a public
issue. Use GitHub's **Report a vulnerability** button (Security tab -> Advisories)
on `livehybrid/stoker`, or email the maintainer at `will@livehybrid.com`. Include
a description, affected versions/commits and a reproduction if you have one. You
will get an acknowledgement, and a fix or mitigation will be coordinated before
any public disclosure.

Stoker is a load generator: treat every deployment as able to push large volumes
of data at a Splunk HEC endpoint. Scope it to targets you own.

## Security model

- **Operator auth.** The API is guarded by a session cookie (local password
  users) or a trusted-proxy SSO header. API tokens (`stk_…`) are bearer
  credentials for CI. Passwords are bcrypt; only a token's sha256 is stored.
  `/api/users` and `/api/tokens` are admin-only.
- **Trusted-proxy header.** The SSO header is honoured **only** when the
  immediate socket peer is inside `STOKER_TRUSTED_PROXIES`. Keep that list as
  tight as possible (ideally the proxy's `/32`). Anything inside it can assert
  any username, including an admin, so it must **not** include the Docker
  overlay/worker network or other app containers.
- **Secrets at rest.** Target HEC tokens and repo credentials are Fernet
  ciphertext under `STOKER_MASTER_KEY` (or `STOKER_MASTER_KEY_FILE`, e.g. a swarm
  secret). The key also signs the session cookie. Set it in production and back
  it up; an unset key is auto-generated per boot and does not survive a restart.
- **Untrusted pack repos.** A registered git repo is untrusted by default:
  `bin/` and `generator =` custom code are stripped unless an admin flags the
  repo `trusted_code`. The bundle builder never follows a symlink out of a pack
  tree, so a hostile pack cannot exfiltrate control-plane files into a bundle.
  Pinned per-SHA snapshots are used to build bundles, so a resync cannot swap the
  payload mid-build.
- **Worker fleet.** Workers authenticate with a short-lived per-run JWT and never
  call the container orchestrator's API. The control plane owns worker identity
  (the lease); a superseded worker is fenced out of ready/heartbeat/final.
- **Portainer.** The SwarmDriver talks to Portainer with a tier-0 API key. Set
  `PORTAINER_VERIFY_TLS=1` (with a CA the host trusts) in production so that key
  is not sent over an unverified TLS channel.

## Hardening checklist for a public/exposed deployment

1. **Close the first-run window.** With zero users, no trusted proxy and no env
   admin, the operator API is open until the first admin is created (the app
   logs a loud warning while it is). Set `STOKER_ADMIN_USER` /
   `STOKER_ADMIN_PASSWORD`, or complete `/api/auth/setup` immediately, before
   exposing the instance.
2. Set a persistent `STOKER_MASTER_KEY` and back it up.
3. Scope `STOKER_TRUSTED_PROXIES` to the reverse proxy only.
4. Set `PORTAINER_VERIFY_TLS=1` and use a trusted certificate.
5. Rate-limit `/api/auth/login` at the reverse proxy (Stoker uses constant-time
   password verification to avoid a username-enumeration timing oracle, but does
   not itself throttle attempts).
6. Bound worker resources with `driver_opts.resources` (`limits.cpus`,
   `limits.memory_mb`) so a run cannot starve its node.

## Known limitations

These are understood trade-offs, not undisclosed bugs:

- **Swarm secret delivery.** The SwarmDriver passes the HEC token and the run JWT
  to workers as service environment variables (visible via `docker service
  inspect` to a Docker-API-privileged user). The Kubernetes driver uses a
  `secretKeyRef` instead. Moving swarm to Docker secrets is tracked for a future
  release.
- **Pre-adoption schema drift.** Boot migrations adopt a fresh or current-schema
  database cleanly. A database from a *pre-Alembic* build with a hand-drifted
  schema is stamped at head without a repair pass; start such installs from a
  fresh database or reconcile the schema manually.
- **Backfill of non-eps specs.** Backfill delivers a `per_day_gb` / count spec at
  the delivery-rate ceiling rather than translating the configured volume into a
  density; prefer an `eps` spec for a precise backfill rate.
