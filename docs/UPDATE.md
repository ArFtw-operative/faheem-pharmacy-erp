# Releases, updates and rollback

## Making a release (developer)

1. Work on `dev`. CI runs on every push: tests on SQLite and PostgreSQL, shellcheck, and a fresh
   install of the real Docker stack with the deployment smoke test.
2. For a release:
   - bump `APP_VERSION` in `app/config.py` (patch = fixes, minor = features, major = breaking workflow);
   - add `## <version> — <date>` to `CHANGELOG.md` (changes, migrations, any `RECONCILE_EXEMPT`);
   - new migrations: additive, run on both engines, `downgrade()` raises ([policy](UPGRADES.md));
   - freeze the release database for future upgrade tests:
     `python scripts/make_release_fixture.py <version> tests/fixtures/releases/<version>.db`.
3. Merge `dev` into `prod` and push. The release workflow runs the tests, refuses a version that is
   already published, builds the image, installs it fresh and smoke-tests it, then pushes
   `ghcr.io/arftw-operative/faheem-pharmacy-erp:<version>` and `:sha-<commit>`, tags `v<version>` and
   creates the GitHub release. A published version is never overwritten.

## How a PC updates

Automatically during the 05:00 maintenance (when **auto-update** is on), or by hand:
desktop **Update ERP**, Control Center → Updates, or `sudo faheem-erp update [VERSION]`.

`update` is transactional:

| Step | If it fails |
|---|---|
| 1. Read the newest `x.y.z` tag from GHCR; pull it; unpack its host tooling to `/opt/faheem-erp/releases/<version>` | nothing changed |
| 2. Rehearse its migrations on a scratch copy of the live database (history fingerprint must be identical) | nothing changed |
| 3. Verified pre-upgrade snapshot | nothing changed |
| 4. Stop web + worker, switch the version, run the migration job, start web + worker | previous version back; snapshot restored if the schema had moved |
| 5. `/health/ready` and the smoke test (database read/write, products, invoice PDF, uploads) | same as 4 |

Every step is logged in `/var/log/faheem-erp/deploy.log`; the outcome shows in Control Center →
Status and in the doctor. `faheem-erp update --check` only reports whether an update exists. An older
version is never installed by `update`.

## Rollback

`sudo faheem-erp rollback` (or Control Center → Updates → Roll back) returns to the previous
version (`FAHEEM_PREVIOUS_VERSION`), or `rollback <version>` to another one:

- if that version's schema matches the database, only the version is switched — no data is lost;
- otherwise the database must come from that update's pre-upgrade snapshot, and **everything written
  since that update is lost**; this needs the typed word `ROLLBACK`.

A safety snapshot is taken before either.

## Images on the PC

The running and the previous version are kept; older images and tooling are removed by the
maintenance cleanup. PostgreSQL, Caddy and WPPConnect images are pinned in `compose.yaml` and change
only with a release.
