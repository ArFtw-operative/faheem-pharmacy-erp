"""Host tooling of the appliance (deploy/appliance) against a modelled Docker: transactional update,
automatic rollback, manual rollback, backup retention, 05:00 maintenance and the boot-time check.
The real stack is exercised by the release workflow and on the pharmacy PC; these tests pin the logic."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
FAKEBIN = Path(__file__).parent / "appliance" / "fakebin"
pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock")


class Box:
    def __init__(self, root: Path):
        self.root = root
        self.home, self.etc, self.data = root / "opt", root / "etc", root / "data"
        self.logs, self.backups, self.state = root / "log", root / "backups", root / "fake"
        for d in (self.home / "releases", self.etc, self.data / "state", self.logs, self.backups / "snapshots", self.state):
            d.mkdir(parents=True)
        shutil.copytree(REPO / "deploy/appliance", self.home / "releases/1.3.0")
        for f in ("compose.yaml", "compose.prod.yaml"):
            shutil.copy(REPO / f, self.home / "releases/1.3.0" / f)
        (self.home / "releases/1.3.0/.complete").touch()
        (self.home / "current").symlink_to("releases/1.3.0")
        self.env_file = self.etc / "faheem.env"
        self.env_file.write_text("FAHEEM_IMAGE=ghcr.io/arftw-operative/faheem-pharmacy-erp\nFAHEEM_VERSION=1.3.0\n"
                                 "FAHEEM_PREVIOUS_VERSION=\nFAHEEM_PORT=8000\nPOSTGRES_PASSWORD=x\nAUTO_UPDATE=true\n"
                                 "DAILY_HOST_REBOOT=true\nBACKUP_RETENTION_DAYS=30\nBACKUP_KEEP_MIN=14\n")
        self.env_file.chmod(0o600)
        (self.etc / "registry.token").write_text("token")
        self.put("db_schema", "1.3.0"); self.put("running", "1.3.0"); self.put("tags", "1.2.0 1.3.0 1.4.0 sha-abc1234")

    def put(self, name, value=""):
        (self.state / name).write_text(value)

    def get(self, name):
        p = self.state / name
        return p.read_text().strip() if p.exists() else ""

    def env(self, key):
        for line in self.env_file.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]

    def run(self, script, *args, check=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FAHEEM_", "PHARMACY_"))}
        env.update(FAHEEM_TEST="1", FAHEEM_HOME=str(self.home), FAHEEM_ETC=str(self.etc), FAHEEM_DATA=str(self.data),
                   FAHEEM_LOGS=str(self.logs), FAHEEM_BACKUPS=str(self.backups), FAHEEM_LOCK=str(self.root / "lock"),
                   FAHEEM_READY_TIMEOUT="1", FAKE_STATE=str(self.state), FAKE_REPO=str(REPO),
                   PATH=f"{FAKEBIN}:{env['PATH']}")
        out = subprocess.run([str(self.home / "current/bin" / script), *args], env=env, capture_output=True, text=True,
                             timeout=120, stdin=subprocess.DEVNULL)
        if check is not None:
            assert (out.returncode == 0) == check, out.stdout + out.stderr
        return out

    def snapshots(self):
        return sorted(p.name for p in (self.backups / "snapshots").iterdir())


@pytest.fixture
def box(tmp_path):
    for f in FAKEBIN.iterdir():
        f.chmod(0o755)
    return Box(tmp_path)


def test_update_switches_version_after_rehearsal_backup_and_checks(box):
    box.run("update.sh", check=True)
    assert box.env("FAHEEM_VERSION") == "1.4.0" and box.env("FAHEEM_PREVIOUS_VERSION") == "1.3.0"
    assert os.readlink(box.home / "current") == "releases/1.4.0"
    assert box.get("running") == "1.4.0" and box.get("db_schema") == "1.4.0"
    snaps = box.snapshots()
    assert len(snaps) == 1 and snaps[0].endswith("-pre-upgrade")
    assert (box.data / "state/pre-upgrade-snapshot.1.4.0").read_text().strip() == snaps[0]
    calls = box.get("calls.log")
    order = [calls.index(k) for k in ("upgrade --check", "app.snapshot create", " up -d", "app.production smoke")]
    assert order == sorted(order)                                            # rehearse → back up → switch → verify
    assert "Up to date" in box.run("update.sh", check=True).stdout          # nothing newer now


def test_failed_health_after_migration_restores_snapshot_and_previous_version(box):
    box.put("unhealthy_1.4.0")
    out = box.run("update.sh", check=False)
    assert "rolled back" in (out.stdout + out.stderr) or "1.3.0 is running again" in out.stderr
    assert box.env("FAHEEM_VERSION") == "1.3.0" and os.readlink(box.home / "current") == "releases/1.3.0"
    assert box.get("restored").endswith("-pre-upgrade")                      # the migrated database was put back
    assert box.get("db_schema") == "1.3.0" and box.get("running") == "1.3.0"


def test_failed_smoke_test_rolls_back(box):
    box.put("fail_smoke_1.4.0")
    box.run("update.sh", check=False)
    assert box.env("FAHEEM_VERSION") == "1.3.0" and box.get("running") == "1.3.0" and box.get("db_schema") == "1.3.0"


def test_failed_rehearsal_changes_nothing(box):
    box.put("fail_rehearsal")
    out = box.run("update.sh", check=False)
    assert "rehearsal" in out.stderr
    assert box.env("FAHEEM_VERSION") == "1.3.0" and box.snapshots() == [] and box.get("running") == "1.3.0"


def test_no_backup_no_update(box):
    box.put("fail_snapshot")
    box.run("update.sh", check=False)
    assert box.env("FAHEEM_VERSION") == "1.3.0" and box.get("running") == "1.3.0"
    assert "compose" not in "\n".join(l for l in box.get("calls.log").splitlines() if " up " in f" {l} ")


def test_never_moves_to_an_older_version(box):
    out = box.run("update.sh", "1.2.0", check=False)
    assert "older" in out.stderr and box.env("FAHEEM_VERSION") == "1.3.0"


def test_rollback_after_update_restores_pre_upgrade_data(box):
    box.run("update.sh", check=True)
    pre = (box.data / "state/pre-upgrade-snapshot.1.4.0").read_text().strip()
    box.run("rollback.sh", "--yes", check=True)
    assert box.env("FAHEEM_VERSION") == "1.3.0" and box.env("FAHEEM_PREVIOUS_VERSION") == "1.4.0"
    assert box.get("restored") == pre and box.get("db_schema") == "1.3.0" and box.get("running") == "1.3.0"


def test_backup_retention_keeps_minimum_and_recent(box):
    store = box.backups / "snapshots"
    for day in ("20200101", "20200102", "20200103", "20200104", "20200105"):
        (store / f"{day}-050000-scheduled").mkdir()
        (store / f"{day}-050000-scheduled" / "x").write_text("x")
        (store / f"{day}-050000-scheduled").chmod(0o500)                     # snapshots are read-only
    box.env_file.write_text(box.env_file.read_text().replace("BACKUP_KEEP_MIN=14", "BACKUP_KEEP_MIN=3"))
    out = box.run("backup.sh", "--reason", "scheduled", check=True)
    new = out.stdout.strip().splitlines()[-1]
    assert box.snapshots() == ["20200104-050000-scheduled", "20200105-050000-scheduled", new]
    assert (box.backups / "host-config/faheem.env").stat().st_mode & 0o777 == 0o600


def test_maintenance_backs_up_updates_and_reboots(box):
    out = box.run("maintenance.sh", check=True)
    assert out.stdout.strip().endswith("REBOOT")
    assert box.env("FAHEEM_VERSION") == "1.4.0"
    assert [s.split("-")[-1] for s in box.snapshots()] == ["scheduled", "upgrade"]


def test_maintenance_skips_update_when_backup_fails(box):
    box.put("fail_snapshot")
    box.run("maintenance.sh")
    assert box.env("FAHEEM_VERSION") == "1.3.0"


def test_boot_check_only_when_last_check_is_old(box):
    (box.data / "state/last-update-check").write_text(str(int(time.time()) - 3600))
    box.run("boot-check.sh", check=True)
    assert box.env("FAHEEM_VERSION") == "1.3.0"
    (box.data / "state/last-update-check").write_text(str(int(time.time()) - 90000))
    box.run("boot-check.sh", check=True)
    assert box.env("FAHEEM_VERSION") == "1.4.0"


def test_scripts_parse():
    for f in [REPO / "deploy/appliance/install.sh", REPO / "deploy/appliance/lib.sh", *(REPO / "deploy/appliance/bin").iterdir()]:
        subprocess.run(["bash", "-n", str(f)], check=True)


def test_compose_security_and_pinning():
    c = yaml.safe_load((REPO / "compose.yaml").read_text())
    s = c["services"]
    assert "ports" not in s["postgres"] and "ports" not in s["whatsapp"]          # never published
    assert s["web"]["ports"] == ["${FAHEEM_BIND:-127.0.0.1}:${FAHEEM_PORT:-8000}:8000"]
    for name, svc in s.items():
        image = svc["image"]
        assert ":" in image and not image.endswith(":latest"), name
        if "logging" in svc:
            assert svc["logging"]["options"]["max-size"]
    assert s["web"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert s["proxy"]["profiles"] == ["lan"]
    timer = (REPO / "deploy/appliance/systemd/faheem-erp-maintenance.timer").read_text()
    assert "05:00:00 Asia/Kolkata" in timer and "Persistent=false" in timer


def test_settings_switches_are_kept_in_the_configuration(box):
    box.run("settings.sh", "set", "auto-update", "off", check=True)
    box.run("settings.sh", "set", "daily-reboot", "off", check=True)
    box.run("settings.sh", "set", "backup-days", "45", check=True)
    assert box.env("AUTO_UPDATE") == "false" and box.env("DAILY_HOST_REBOOT") == "false" and box.env("BACKUP_RETENTION_DAYS") == "45"
    assert box.run("settings.sh", "set", "backup-days", "2").returncode != 0          # below the floor
    assert box.run("settings.sh", "set", "maintenance-time", "25:00").returncode != 0
    shown = box.run("settings.sh", "show", check=True).stdout
    assert "auto-update        off" in shown and "backup-days        45" in shown
    out = box.run("maintenance.sh", check=True)                                       # no reboot, no update now
    assert "REBOOT" not in out.stdout and box.env("FAHEEM_VERSION") == "1.3.0"


def test_doctor_diagnoses_from_live_state_and_proposes_fixes(box):
    import json

    box.put("running", "")                                                            # ERP down
    out = box.run("doctor.sh", "--json")
    findings = json.loads(out.stdout)
    by_title = {f["title"]: f for f in findings}
    assert out.returncode == 1                                                        # a FAIL remains
    assert any(f["severity"] == "FAIL" and f["fix"] == "start" for f in findings)    # not answering → start it
    assert any(t.startswith("No backup yet") and f["fix"] == "backup" for t, f in by_title.items())
    assert any("Update available: 1.3.0 → 1.4.0" in t and f["fix"] == "update" for t, f in by_title.items())
    assert all({"severity", "title", "detail", "fix"} <= f.keys() for f in findings)
    box.run("doctor.sh", "--fix", "backup")                                          # applies only what was asked
    assert len(box.snapshots()) == 1 and box.env("FAHEEM_VERSION") == "1.3.0"


def test_update_without_registry_token_uses_anonymous_access(box):
    (box.etc / "registry.token").unlink()                                             # public images
    box.run("update.sh", check=True)
    assert box.env("FAHEEM_VERSION") == "1.4.0"
