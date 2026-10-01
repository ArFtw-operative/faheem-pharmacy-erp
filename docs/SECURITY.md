# Security

## Repository and data policy

- The complete application repository is **private**.
- No production credentials are committed. `.env` files are ignored; `.env.example`
  contains placeholders only.
- Demonstration data is **synthetic** — the demo seed creates fictional products,
  suppliers, customers, purchases and sales, and the demo database is excluded from
  version control.
- Never commit: `.env` files, databases, uploads, logs, backups, exports, or any real
  customer / patient / supplier / employee data.

A `.gitignore` enforces these exclusions and the demo seed refuses to run unless
explicitly opted in with `PHARMACY_DEMO_SEED=1`.

## Application security features

- **Password hashing** — bcrypt (`app/security.py`).
- **Sessions** — signed session cookies; login and logout are audited.
- **RBAC** — 55 granular permissions across 12 modules, enforced per route via
  `require_permission`; roles are seeded but editable at runtime.
- **Idle / lock** — an idle screen and a workspace lock with configurable timeouts.
- **Audit log** — every data-changing operation records who did what, with before/after
  snapshots.
- **Sensitive actions** — deleting inventory and large refunds require an administrator
  password / manager approval.

## Secret scanning

Before publishing, scan the tree and history for secrets (`password`, `secret`,
`token`, `api_key`, `authorization`, `smtp`, `database_url`, `private_key`). CI runs a
lightweight scan on every push. If a secret is ever committed, remove it from history
and rotate the credential — dropping it from the latest commit is not sufficient.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner rather than
opening a public issue.

## Scope of claims

This document describes the security controls actually present in the code. It does not
claim any certification or guarantee. The application is designed for a trusted local
deployment; exposing it to the public internet requires additional hardening
(TLS, a reverse proxy, hardened secrets, and network restrictions) outside this
repository's current scope.
