"""Faheem Pharmacy ERP — administration from the command line.

The ERP has no Admin, Settings or Backups screens; these tasks are done here.

    python scripts/manage.py user list
    python scripts/manage.py user add USERNAME --name "Full Name" [--role Administrator] [--password P]
    python scripts/manage.py user passwd USERNAME [--password P]
    python scripts/manage.py user disable USERNAME | enable USERNAME
    python scripts/manage.py user reset-2fa USERNAME      # lost phone: new QR at next login
    python scripts/manage.py user unlock USERNAME         # after too many wrong passwords
    python scripts/manage.py role list

    python scripts/manage.py setting list
    python scripts/manage.py setting get KEY
    python scripts/manage.py setting set KEY VALUE        # e.g. pharmacy_name, max_discount_pct
    python scripts/manage.py logo PATH/TO/logo.png

    python scripts/manage.py version [--json]             # app version, schema revision, pending migrations
    python scripts/manage.py upgrade --check              # staging rehearsal on a copy (live data untouched)
    python scripts/manage.py upgrade                      # guarded upgrade: snapshot → migrate → reconcile
    python scripts/manage.py deployments                  # upgrade / release history
    python scripts/manage.py purchases revalidate         # re-check open lines of unposted purchases (after a rule change)

Snapshots of the whole ERP (database, uploads, config) are taken with
scripts/snapshot.sh (create | list | verify | restore | drill | where).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Role, Setting, User  # noqa: E402
from app.security import find_user  # noqa: E402
from app.services import settings_service, upgrade_service, user_service  # noqa: E402


def _user(db, username: str) -> User:
    user = find_user(db, username)
    if user is None:
        raise SystemExit(f"No such user: {username}")
    return user


def _password(given: str | None) -> str | None:
    if given:
        return given
    if sys.stdin.isatty():
        first = getpass.getpass("New password (blank = generate): ")
        if first and first != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        return first or None
    return None


def cmd_user(args) -> None:
    with SessionLocal() as db:
        if args.action == "list":
            for u in db.scalars(select(User).order_by(User.id)):
                print(f"{u.employee_id:10} {u.username:20} {u.full_name:28} {(u.role.name if u.role else '-'):16} "
                      f"{'active' if u.is_active else 'disabled'}")
            return
        if args.action == "add":
            role = db.scalar(select(Role).where(Role.name == args.role))
            if role is None:
                raise SystemExit(f"No such role: {args.role} (see: role list)")
            given = _password(args.password)
            user, password = user_service.create_user(db, username=args.username, full_name=args.name or args.username,
                                                      role_id=role.id, password=given,
                                                      must_change_password=not (args.keep_password and given))
            db.commit()
            shown = "(as given)" if given else password      # a password the owner typed is never echoed
            print(f"Created {user.username} ({user.employee_id}) · password: {shown} · two-step sign-in is set up at first login")
        elif args.action == "passwd":
            password = user_service.reset_password(db, _user(db, args.username), new_password=_password(args.password))
            db.commit()
            print(f"Password reset for {args.username}: {password} · must change at next login")
        elif args.action == "reset-2fa":            # lost phone and recovery codes: enrol again at next login
            u = _user(db, args.username)
            u.mfa_enabled, u.mfa_secret, u.mfa_recovery, u.mfa_last_step = False, "", None, None
            from app import audit
            audit.record(db, action=audit.A_UPDATE, entity_type="user", entity_id=u.id, details="Two-factor reset by administrator (CLI)")
            db.commit()
            print(f"Two-step sign-in reset for {u.username}: a new QR code is shown at the next login")
        elif args.action == "unlock":
            u = _user(db, args.username)
            u.failed_logins, u.locked_until = 0, None
            db.commit()
            print(f"{u.username} unlocked")
        elif args.action in ("disable", "enable"):
            user_service.set_active(db, _user(db, args.username), args.action == "enable")
            db.commit()
            print(f"{args.username} {args.action}d")


def cmd_role(args) -> None:
    with SessionLocal() as db:
        for r in db.scalars(select(Role).order_by(Role.id)):
            print(f"{r.name:20} {len(r.permissions)} permissions")


def cmd_setting(args) -> None:
    with SessionLocal() as db:
        if args.action == "list":
            for s in db.scalars(select(Setting).where(~Setting.key.like("keymap:%")).order_by(Setting.key)):
                print(f"{s.key:32} {s.value}")
        elif args.action == "get":
            print(settings_service.get_setting(db, args.key))
        elif args.action == "set":
            if args.value is None:
                raise SystemExit("setting set KEY VALUE")
            settings_service.set_setting(db, args.key, args.value)
            db.commit()
            print(f"{args.key} = {args.value}")


def cmd_logo(args) -> None:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"No such file: {path}")
    with SessionLocal() as db:
        settings_service.save_logo(db, path.name, path.read_bytes())
        db.commit()
    print("Logo updated")


def cmd_version(args) -> None:
    import json

    from app.config import APP_BUILD, APP_VERSION

    st = upgrade_service.state()
    info = {"app_version": APP_VERSION, "build": APP_BUILD, "schema_code": st["code"], "schema_database": st["database"],
            "state": st["state"], "pending": upgrade_service.pending_revisions() if st["state"] == "pending" else []}
    if args.json:
        print(json.dumps(info))
        return
    print(f"Faheem Pharmacy {APP_VERSION} (build {APP_BUILD})")
    print(f"Schema: code {','.join(st['code'])} · database {','.join(st['database']) or '—'} · {st['state']}")
    if info["pending"]:
        print("Pending migrations: " + ", ".join(info["pending"]))


def cmd_upgrade(args) -> None:
    if args.check:
        out = upgrade_service.check()
        if out["state"] in ("fresh", "current"):
            print(f"Nothing to migrate (database is {out['state']}).")
            return
        print(f"Rehearsed {len(out['migrations'])} migration(s) on a copy: {', '.join(out['migrations'])}")
        if out["problems"]:
            print("FAILED — the upgrade would change or lose history:")
            for p in out["problems"]:
                print("  " + p)
            raise SystemExit(1)
        print(f"OK — {out['checked']} history figures identical after the migrations. Safe to upgrade.")
        return
    out = upgrade_service.upgrade()
    print({"installed": "New database created.", "current": "Database already up to date."}.get(out["status"])
          or f"Upgraded: {len(out['migrations'])} migration(s), snapshot {out['backup']}, {out['reconciled']} history figures reconciled.")


def cmd_purchases(args) -> None:
    from app.models import Purchase
    from app.services import purchasing

    with SessionLocal() as db:
        docs = db.scalars(select(Purchase).where(Purchase.status.in_(("DRAFT", "PARTIAL")))).all()
        changed = 0
        for p in docs:
            for line in p.items:
                if line.status in purchasing.DONE:
                    continue                                   # posted / closed lines are history: never re-derived
                before = line.status
                purchasing.refresh_line(db, p, line)
                changed += line.status != before
            purchasing._refresh_totals(p)
        db.commit()
        print(f"Re-checked {sum(len([l for l in p.items if l.status not in purchasing.DONE]) for p in docs)} open line(s) "
              f"in {len(docs)} unposted purchase(s); {changed} changed status.")


def cmd_deployments(args) -> None:
    rows = upgrade_service.history()
    for r in rows:
        print(f"{r['at']:20} {r['version']:8} {r['status']:12} {r['from'] or '—':>14} → {r['to'] or '—':14} {r['backup'] or ''}")
    if not rows:
        print("No deployments recorded yet.")


def cmd_reset_test_data(args) -> None:
    """Remove test stock, products, purchases and sales; keep customers, users, settings (WhatsApp)."""
    from app.database import SessionLocal
    from app.services import data_reset

    with SessionLocal() as db:
        rows = {k: v for k, v in data_reset.counts(db).items() if v and k not in data_reset.CUSTOMER_TABLES}
        print("Will remove:", ", ".join(f"{k} {v}" for k, v in rows.items()) or "nothing (already empty)")
        if not args.yes:
            print("Preview only — add --yes to remove (customers, users and settings are kept).")
            return
        data_reset.wipe(db, keep_customers=True)
        db.commit()
        print("Test data removed. Kept: customers and follow-ups, users, roles, settings (WhatsApp included).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("user")
    u.add_argument("action", choices=["list", "add", "passwd", "disable", "enable", "reset-2fa", "unlock"])
    u.add_argument("username", nargs="?")
    u.add_argument("--name", default="")
    u.add_argument("--role", default="Administrator")
    u.add_argument("--password")
    u.add_argument("--keep-password", action="store_true", help="the given password stays (no forced change)")
    sub.add_parser("role").add_argument("action", choices=["list"])
    s = sub.add_parser("setting")
    s.add_argument("action", choices=["list", "get", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    sub.add_parser("logo").add_argument("path")
    sub.add_parser("version").add_argument("--json", action="store_true")
    sub.add_parser("upgrade").add_argument("--check", action="store_true", help="rehearse on a copy; change nothing")
    sub.add_parser("deployments")
    sub.add_parser("purchases").add_argument("action", choices=["revalidate"])
    sub.add_parser("reset-test-data").add_argument("--yes", action="store_true", help="actually remove (otherwise preview)")
    args = parser.parse_args()
    if args.cmd == "user" and args.action != "list" and not args.username:
        parser.error("username required")
    try:
        if args.cmd in ("version", "upgrade", "deployments"):   # never migrate as a side effect of looking
            {"version": cmd_version, "upgrade": cmd_upgrade, "deployments": cmd_deployments}[args.cmd](args)
            return
        init_db()
        {"user": cmd_user, "role": cmd_role, "setting": cmd_setting, "logo": cmd_logo, "purchases": cmd_purchases,
         "reset-test-data": cmd_reset_test_data}[args.cmd](args)
    except upgrade_service.UpgradeError as exc:
        raise SystemExit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
