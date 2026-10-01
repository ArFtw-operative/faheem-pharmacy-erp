"""WhatsApp gateway providers.

POS and Sales History never talk to a provider directly: they call
``whatsapp.service.send_invoice(sale_id, phone)``. A provider only knows how to
connect one WhatsApp account and deliver a text, a PDF and an image, so
WPPConnect can later be replaced by the official WhatsApp Business (Cloud) API by
adding another class with the same methods.
"""
from __future__ import annotations

import base64
import ipaddress
import os
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

# Connection states shown in Settings and to the POS
CONNECTED, DISCONNECTED, QR_REQUIRED, STARTING, NOT_CONFIGURED, ERROR = (
    "CONNECTED", "DISCONNECTED", "QR_REQUIRED", "STARTING", "NOT_CONFIGURED", "ERROR")


class TemporaryError(Exception):
    """Network trouble, gateway restarting, WhatsApp disconnected: worth retrying."""


class PermanentError(Exception):
    """The number is not on WhatsApp, bad request…: retrying will not help."""


@dataclass
class Connection:
    state: str
    detail: str = ""
    qr: str | None = None          # data:image/png;base64,… — only ever returned to administrators


class Provider:
    name = "base"

    def connection(self) -> Connection:                      # pragma: no cover - interface
        raise NotImplementedError

    def connect(self) -> Connection:                         # pragma: no cover
        raise NotImplementedError

    def logout(self) -> None:                                # pragma: no cover
        raise NotImplementedError

    def send_text(self, phone: str, text: str) -> str:       # pragma: no cover
        raise NotImplementedError

    def send_file(self, phone: str, filename: str, data: bytes, mime: str, caption: str = "") -> str:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- WPPConnect
def _private_host(host: str) -> bool:
    """Only loopback / private-network gateways are allowed: the API must never be reached over the internet."""
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, None)}
        except OSError:
            return False
    return bool(addrs) and all(a.is_loopback or a.is_private for a in addrs)


class GatewayBusy(Exception):
    """The gateway accepted the request but has not answered yet (starting its browser)."""


class WPPConnect(Provider):
    """https://github.com/wppconnect-team/wppconnect-server (run locally in Docker).

    Configuration comes only from the environment (never from code or the database):
        PHARMACY_WPP_URL       http://127.0.0.1:21465  (loopback / private address only)
        PHARMACY_WPP_SECRET    the server's SECRET_KEY
        PHARMACY_WPP_SESSION   session name (default faheem-pharmacy)
    The bearer token is derived from the secret on demand and kept in memory only.
    """
    name = "wppconnect"
    _token_lock = threading.Lock()
    _tokens: dict[tuple[str, str], str] = {}

    def __init__(self, url: str | None = None, secret: str | None = None, session: str | None = None, transport=None):
        self.url = (url if url is not None else os.environ.get("PHARMACY_WPP_URL", "http://127.0.0.1:21465")).rstrip("/")
        self.secret = secret if secret is not None else os.environ.get("PHARMACY_WPP_SECRET", "")
        self.session = session or os.environ.get("PHARMACY_WPP_SESSION", "faheem-pharmacy")
        self._transport = transport

    # -- plumbing
    def problem(self) -> str:
        if not self.secret:
            return "PHARMACY_WPP_SECRET is not set (see docs/WHATSAPP.md)"
        host = urlparse(self.url).hostname or ""
        if not _private_host(host):
            return f"PHARMACY_WPP_URL host “{host}” is not a local or private-network address — refused"
        return ""

    def _client(self):
        import httpx

        return httpx.Client(base_url=self.url, timeout=httpx.Timeout(30.0, connect=5.0), transport=self._transport)

    def _token(self, fresh: bool = False) -> str:
        key = (self.url, self.session)
        with self._token_lock:
            if fresh or key not in self._tokens:
                with self._client() as c:
                    r = c.post(f"/api/{self.session}/{self.secret}/generate-token")
                if r.status_code >= 400:
                    raise TemporaryError(f"WhatsApp gateway refused the secret key (HTTP {r.status_code})")
                data = r.json()
                # the Authorization header takes the bare token; "full" is "<session>:<token>" for URL use
                self._tokens[key] = data.get("token") or str(data.get("full") or "").split(":", 1)[-1]
            return self._tokens[key]

    def _call(self, method: str, path: str, json: dict | None = None, timeout: float | None = None):
        import httpx

        why = self.problem()
        if why:
            raise TemporaryError(why)
        for attempt in (0, 1):
            try:
                with self._client() as c:
                    r = c.request(method, f"/api/{self.session}/{path}", json=json,
                                  headers={"Authorization": f"Bearer {self._token(fresh=attempt == 1)}"},
                                  **({"timeout": timeout} if timeout else {}))
            except httpx.TimeoutException as exc:
                raise GatewayBusy(f"WhatsApp gateway is still working ({exc.__class__.__name__})") from exc
            except httpx.HTTPError as exc:
                raise TemporaryError(f"WhatsApp gateway not reachable at {self.url} ({exc.__class__.__name__})") from exc
            if r.status_code == 401 and attempt == 0:
                continue                                    # token expired: derive a new one once
            return r
        return r

    # -- connection
    def connection(self) -> Connection:
        why = self.problem()
        if why:
            return Connection(NOT_CONFIGURED, why)
        try:
            r = self._call("GET", "status-session")
        except GatewayBusy:
            return Connection(STARTING, "Starting the WhatsApp session…")
        except TemporaryError as exc:
            return Connection(ERROR, str(exc))
        if r.status_code in (401, 403):
            return Connection(ERROR, f"WhatsApp gateway refused the ERP's access (HTTP {r.status_code}) — check PHARMACY_WPP_SECRET")
        if r.status_code >= 500:
            return Connection(ERROR, f"WhatsApp gateway error (HTTP {r.status_code})")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        status = str(data.get("status") or "").upper()
        if status in ("CONNECTED", "INCHAT", "ISLOGGED", "MAIN"):
            return Connection(CONNECTED, "WhatsApp is connected")
        if status in ("QRCODE", "QR_CODE", "NOTLOGGED"):
            return Connection(QR_REQUIRED, "Scan the QR code with WhatsApp → Linked devices", data.get("qrcode"))
        if status in ("INITIALIZING", "STARTING", "OPENING", "PAIRING", "SYNCING"):
            return Connection(STARTING, "Starting the WhatsApp session…", data.get("qrcode"))
        return Connection(DISCONNECTED, "WhatsApp is not connected")

    def connect(self) -> Connection:
        why = self.problem()
        if why:
            return Connection(NOT_CONFIGURED, why)
        try:
            # do not hold the request until the QR exists: the first start launches a browser and loads
            # WhatsApp Web (up to a minute); the Settings page polls status-session for the QR instead
            r = self._call("POST", "start-session", {"waitQrCode": False}, timeout=60.0)
        except GatewayBusy:
            return Connection(STARTING, "Starting the WhatsApp session… the first start can take a minute")
        except TemporaryError as exc:
            return Connection(ERROR, str(exc))
        if r.status_code in (401, 403):
            return Connection(ERROR, f"WhatsApp gateway refused the ERP's access (HTTP {r.status_code}) — check PHARMACY_WPP_SECRET")
        data = r.json() if r.content else {}
        status = str(data.get("status") or "").upper()
        if status == "CONNECTED":
            return Connection(CONNECTED, "WhatsApp is connected")
        if data.get("qrcode"):
            return Connection(QR_REQUIRED, "Scan the QR code with WhatsApp → Linked devices", data["qrcode"])
        return self.connection()

    def logout(self) -> None:
        r = self._call("POST", "logout-session")
        if r.status_code >= 500:
            raise TemporaryError(f"Logout failed (HTTP {r.status_code})")

    # -- sending
    @staticmethod
    def _result(r) -> str:
        if r.status_code in (200, 201):
            try:
                data = r.json()
            except ValueError:
                return ""
            if str(data.get("status", "")).lower() in ("success", "ok") or data.get("response"):
                resp = data.get("response")
                first = resp[0] if isinstance(resp, list) and resp else resp
                return str((first or {}).get("id", "") if isinstance(first, dict) else "")
        text = ""
        try:
            data = r.json()
            text = str(data.get("message") or data.get("error") or data)
        except ValueError:
            text = r.text[:300]
        low = text.lower()
        if r.status_code in (400, 404, 422) and any(w in low for w in ("not exist", "invalid", "not registered", "wid", "no account")):
            raise PermanentError(f"This number is not on WhatsApp ({text[:160]})")
        if "disconnected" in low or "not connected" in low or "session" in low and "start" in low:
            raise TemporaryError("WhatsApp disconnected — reconnect from Settings.")
        raise TemporaryError(f"WhatsApp gateway error (HTTP {r.status_code}): {text[:160]}")

    def send_text(self, phone: str, text: str) -> str:
        return self._result(self._call("POST", "send-message", {"phone": phone, "isGroup": False, "message": text}))

    def send_file(self, phone: str, filename: str, data: bytes, mime: str, caption: str = "") -> str:
        b64 = f"data:{mime};base64," + base64.b64encode(data).decode()
        return self._result(self._call("POST", "send-file-base64",
                                       {"phone": phone, "isGroup": False, "filename": filename, "caption": caption, "base64": b64}))


def default_provider() -> Provider:
    return WPPConnect()
