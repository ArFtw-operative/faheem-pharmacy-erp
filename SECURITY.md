# Security

Faheem Pharmacy is a self-hosted application. Please review the full policy in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Summary

- The complete application repository is **private**.
- No production credentials, databases, uploads, logs or backups are committed;
  `.env` files are ignored and [`.env.example`](.env.example) contains placeholders only.
- Demonstration data is **synthetic**. The demo seed refuses to run unless explicitly
  opted in with `PHARMACY_DEMO_SEED=1`.
- The application hashes passwords with bcrypt, uses signed session cookies, enforces
  RBAC on every route, and writes an audit record for every data change.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner rather than
opening a public issue.

## Claims

This repository makes no certification or security-guarantee claims. It is intended for
a trusted local deployment; exposing it publicly requires additional hardening (TLS,
reverse proxy, hardened secrets, network restrictions) outside the current scope.
