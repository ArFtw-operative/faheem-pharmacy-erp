"""Faheem Remote Support (deploy/support): the real scripts against a local HTTPS "support server", a
stand-in agent that really connects to it, and systemctl / systemd-run stand-ins. The real MeshCentral
server and agent were exercised end to end separately (docs/REMOTE-SUPPORT.md, "Verification")."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "deploy" / "support"
FAKEBIN = Path(__file__).parent / "support" / "fakebin"
pytestmark = pytest.mark.skipif(not shutil.which("openssl") or not shutil.which("ss") or not shutil.which("flock"),
                                reason="needs openssl, ss (iproute2) and flock")

MSH = ("MeshName=Hyderabad\nMeshType=2\nMeshID=0xSECRETMESHID0123456789\nServerID=SECRETSERVERID\n"
       "MeshServer=wss://127.0.0.1:{port}/agent.ashx\ntranslation={{\"en\":{{}}}}\n")
AGENT = """#!/usr/bin/env bash
# stand-in agent: keeps one connection open to the support server, like the MeshCentral agent
# (and, like it, keeps retrying while the server is unreachable)
exec -a "$0" python3 -c 'import socket,ssl,sys,time
c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
while True:
    try:
        s = c.wrap_socket(socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2))
        time.sleep(3600)
    except OSError:
        time.sleep(1)' {port}
"""


class Server:
    """A tiny HTTPS server: /meshsettings → the enrollment file, /meshagents → an agent, / → 200."""

    def __init__(self, tmp: Path):
        cert, key = tmp / "cert.pem", tmp / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                        "-days", "2", "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/meshsettings"):
                    body = MSH.format(port=outer.port).encode()
                elif self.path.startswith("/meshagents"):
                    body = b"\x7fELF" + b"\0" * 600_000          # what the installer accepts as an agent
                else:
                    body = b"ok"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()


class Box:
    def __init__(self, tmp: Path, server: Server):
        self.tmp, self.server = tmp, server
        self.dirs = {k: tmp / k for k in ("home", "etc", "state", "logs", "systemd", "bin", "fake")}
        for d in self.dirs.values():
            d.mkdir()
        self.pkg = tmp / "pkg"
        shutil.copytree(PKG, self.pkg)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SUPPORT_", "FAHEEM_", "PKEXEC"))}
        env.update(FAHEEM_TEST="1", SUPPORT_HOME=str(self.dirs["home"]), SUPPORT_ETC=str(self.dirs["etc"]),
                   SUPPORT_STATE=str(self.dirs["state"]), SUPPORT_LOGS=str(self.dirs["logs"]),
                   SUPPORT_SYSTEMD=str(self.dirs["systemd"]), SUPPORT_BIN_DIR=str(self.dirs["bin"]),
                   FAKE_STATE=str(self.dirs["fake"]), SUPPORT_CURL_INSECURE="1", SUPPORT_CONNECT_WAIT="8",
                   PATH=f"{FAKEBIN}:{env['PATH']}", **extra)
        return env

    def run(self, *args, script=None, check=True, **extra):
        cmd = [str(script or self.dirs["bin"] / "faheem-support"), *args]
        out = subprocess.run(cmd, env=self.env(**extra), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if check:
            assert out.returncode == 0, out.stdout + out.stderr
        return out

    def install(self, *extra):
        out = self.run("install", "--enroll-url", f"https://127.0.0.1:{self.server.port}/meshsettings?id=x",
                       "--name", "hyd faheem pharmacy", *extra, script=self.pkg / "bin" / "faheem-support")
        agent = self.dirs["home"] / "meshagent" / "meshagent"
        agent.write_text(AGENT.format(port=self.server.port)); agent.chmod(0o755)
        return out

    def state(self):
        return json.loads(self.run("status", "--json").stdout)

    @property
    def log(self):
        p = self.dirs["logs"] / "support.log"
        return p.read_text() if p.exists() else ""

    def timer(self):
        p = self.dirs["fake"] / "units" / "faheem-support-expiry.timer"
        return p.read_text().splitlines() if p.exists() else []

    def cleanup(self):
        subprocess.run(["pkill", "-f", f"^{self.dirs['home']}/meshagent/meshagent"], capture_output=True)


@pytest.fixture
def box(tmp_path):
    srv = Server(tmp_path)
    b = Box(tmp_path, srv)
    yield b
    b.cleanup()
    srv.stop()


def test_install_names_the_device_keeps_secrets_private_and_stays_off(box):
    out = box.install()
    assert "installed as HYD-FAHEEM-PHARMACY" in out.stdout and "reachable" in out.stdout
    msh = (box.dirs["home"] / "meshagent" / "meshagent.msh")
    text = msh.read_text()
    assert "agentName=HYD-FAHEEM-PHARMACY" in text and "translation=" not in text and "MeshID=0xSECRET" in text
    assert oct(msh.stat().st_mode & 0o777) == "0o600" and oct((box.dirs["home"] / "meshagent").stat().st_mode & 0o777) == "0o700"
    conf = (box.dirs["etc"] / "support.env").read_text()
    assert "SUPPORT_NAME=HYD-FAHEEM-PHARMACY" in conf and "SUPPORT_DURATION_MIN=60" in conf and "SECRET" not in conf
    assert box.state()["state"] == "DISABLED"
    assert "SECRET" not in box.log                                       # no identifiers in the log
    systemctl = (box.dirs["fake"] / "systemctl.log").read_text() if (box.dirs["fake"] / "systemctl.log").exists() else ""
    assert "start faheem-support-agent" not in systemctl                 # installing never turns support on


def test_bad_enrollment_and_bad_names_are_refused(box, tmp_path):
    bad = tmp_path / "bad.msh"; bad.write_text("hello\n")
    out = box.run("install", "--enroll-file", str(bad), script=box.pkg / "bin" / "faheem-support", check=False)
    assert out.returncode != 0 and "not a MeshCentral agent settings" in out.stderr
    out = box.run("install", "--enroll-url", f"https://127.0.0.1:{box.server.port}/meshsettings", "--name", "x",
                  script=box.pkg / "bin" / "faheem-support", check=False)
    assert out.returncode != 0 and "--name" in out.stderr


def test_enable_connects_for_the_window_and_disable_ends_it(box):
    box.install()
    out = box.run("enable")
    assert "Status:     Connected" in out.stdout and "HYD-FAHEEM-PHARMACY" in out.stdout
    s = box.state()
    assert s["state"] == "CONNECTED" and 3500 < s["seconds_left"] <= 3600
    delay, command = box.timer()
    assert delay == "60m" and command.endswith("faheem-support disable --reason expired")
    out = box.run("disable")
    assert "OFF" in out.stdout and box.state()["state"] == "DISABLED"
    assert not (box.dirs["state"] / "session.json").exists()
    assert "support enabled for 60 min" in box.log and "support disabled (manual)" in box.log


def test_the_timer_ends_support_and_the_desktop_cannot_extend_it(box):
    box.install()
    box.run("enable")
    _, command = box.timer()
    subprocess.run(command.split(), env=box.env(), check=True, capture_output=True)     # what systemd runs at the end
    assert box.state()["state"] == "DISABLED" and "support window expired" in box.log
    # from the desktop (pkexec): only enable / disable / status, with the fixed duration
    assert "fixed" in box.run("enable", "--duration", "600", check=False, PKEXEC_UID=str(os.getuid())).stderr
    assert "only enable, disable and status" in box.run("repair", check=False, PKEXEC_UID=str(os.getuid())).stderr
    assert box.run("enable", "--duration", "3", check=False).returncode != 0          # 5–1440 minutes
    box.run("enable", "--duration", "120")
    assert box.timer()[0] == "120m"


def test_status_tells_server_trouble_and_agent_trouble_apart(box):
    box.install()
    box.run("enable")
    box.cleanup()                                                         # the agent died
    time.sleep(0.5)
    assert box.state()["state"] == "AGENT_ERROR"
    box.run("disable")
    box.server.stop()
    box.run("enable", check=False)
    assert box.state()["state"] in ("SERVER_UNAVAILABLE", "CONNECTING")


def test_a_restart_restores_the_window_for_the_time_left_only_when_allowed(box):
    box.install()
    box.run("enable")
    box.cleanup(); shutil.rmtree(box.dirs["fake"] / "units")              # the PC restarted
    box.run("resume")
    s = box.state()
    assert s["state"] == "CONNECTED" and box.timer()[0].endswith("s") and "restored after a restart" in box.log
    conf = box.dirs["etc"] / "support.env"
    conf.write_text(conf.read_text().replace("SUPPORT_RESUME_AFTER_REBOOT=true", "SUPPORT_RESUME_AFTER_REBOOT=false"))
    box.cleanup(); shutil.rmtree(box.dirs["fake"] / "units")
    box.run("resume")
    assert box.state()["state"] == "DISABLED"


def test_upgrade_keeps_identity_and_enrollment(box):
    box.install()
    ident = box.dirs["home"] / "meshagent" / "meshagent.db"; ident.write_text("identity")
    newer = box.tmp / "newer"; shutil.copytree(PKG, newer); (newer / "VERSION").write_text("1.0.1\n")
    out = box.run("upgrade", "--from", str(newer))
    assert "1.0.0 → 1.0.1" in out.stdout and ident.read_text() == "identity"
    assert "agentName=HYD-FAHEEM-PHARMACY" in (box.dirs["home"] / "meshagent" / "meshagent.msh").read_text()
    older = box.tmp / "older"; shutil.copytree(PKG, older); (older / "VERSION").write_text("0.9.0\n")
    assert box.run("upgrade", "--from", str(older), check=False).returncode != 0


@pytest.mark.skipif(not shutil.which("visudo") and not Path("/usr/sbin/visudo").exists(), reason="needs visudo")
@pytest.mark.parametrize("mode", ["limited", "full"])
def test_sudo_rules_are_valid(box, mode):
    text = box.run("sudoers-text", mode, script=PKG / "bin" / "faheem-support").stdout
    f = box.tmp / f"sudo-{mode}"; f.write_text(text)
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    assert subprocess.run([visudo, "-cf", str(f)], capture_output=True).returncode == 0
    if mode == "limited":
        assert "NOPASSWD" in text and "faheem-support-admin" in text and " ALL\n" not in text


def test_the_admin_helper_refuses_anything_outside_its_list(box):
    box.install()
    helper = PKG / "bin" / "faheem-support-admin"
    for args in (["sh"], ["restart", "anything"], ["journal", "sshd"], ["erp", "reset-test-data"], ["erp", "update", "9.9.9"]):
        out = box.run(*args, script=helper, check=False)
        assert out.returncode == 2 and "not allowed" in out.stderr, args
    assert "admin refused" in box.log


def test_erp_updates_never_touch_remote_support():
    update = (REPO / "deploy/appliance/bin/update.sh").read_text()
    lib = (REPO / "deploy/appliance/lib.sh").read_text()
    code = lambda text: "\n".join(l.split("#", 1)[0] for l in text.splitlines())     # without comments
    assert "faheem-support" not in code(update) and "support-package" not in code(update)
    # releases carry the package (for an explicit "faheem-support upgrade") but never install it
    assert "support-package/" in code(lib) and "faheem-support" not in code(lib)
    agent_unit = (PKG / "systemd/faheem-support-agent.service").read_text()
    assert "[Install]" not in agent_unit.split("# No [Install]")[0]      # never started at boot
