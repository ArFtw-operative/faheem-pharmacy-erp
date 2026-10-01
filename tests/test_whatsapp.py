"""WhatsApp invoices: queued (never blocking a sale), delivered with retries, resend = same invoice,
admin-only pairing, and the WPPConnect client's requests checked against its API."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from app.config import UPLOAD_DIR
from app.models import Role, Sale, WhatsAppMessage
from app.services import inventory_service as inv
from app.services import sales_service, user_service
from app.services.customer_service import create_customer
from app.services.whatsapp import provider as P
from app.services.whatsapp import service as wa
from app.utils import utcnow
from tests.conftest import login


class FakeGateway(P.Provider):
    name = "fake"

    def __init__(self):
        self.state, self.sent, self.fail = P.CONNECTED, [], []      # fail: exceptions raised by the next sends

    def connection(self):
        return P.Connection(self.state, qr="data:image/png;base64,QR" if self.state == P.QR_REQUIRED else None)

    def connect(self):
        return self.connection()

    def logout(self):
        self.state = P.DISCONNECTED

    def send_text(self, phone, text):
        return self.send_file(phone, "", b"", "text/plain", text)

    def send_file(self, phone, filename, data, mime, caption=""):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((phone, filename, data, mime, caption))
        return f"msg-{len(self.sent)}"


@pytest.fixture
def gw():
    fake = FakeGateway()
    wa._provider_override = fake
    wa._cache.update(at=0.0, conn=None)
    yield fake
    wa._provider_override = None
    wa._cache.update(at=0.0, conn=None)


def _sale(db, mobile="98765 43210"):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)
    inv.add_or_update_batch(db, item, batch_no="B1", quantity=5, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    cust = create_customer(db, name="Ratz", mobile=mobile) if mobile else None
    db.commit()
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}], customer_id=cust.id if cust else None)
    db.commit()
    return sale


def _due(db):
    for m in db.query(WhatsAppMessage).filter(WhatsAppMessage.status == "QUEUED"):
        m.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()


def test_indian_numbers_are_normalised():
    for raw in ("98765 43210", "+91-98765-43210", "098765 43210", "919876543210", "+91 9876543210"):
        assert wa.normalize_phone(raw) == "919876543210"
    for bad in ("12345", "5876543210", "", "9876"):
        with pytest.raises(wa.WhatsAppError):
            wa.normalize_phone(bad)


def test_send_is_queued_and_delivered_with_the_stored_pdf(db, gw):
    sale = _sale(db)
    gw.state = P.DISCONNECTED                     # WhatsApp down: queuing still works, the sale is untouched
    msg = wa.send_invoice(db, sale.id)
    db.commit()
    assert msg.status == "QUEUED" and msg.customer_phone == "919876543210" and gw.sent == []
    assert f"#{sale.invoice_no}" in msg.message_text and "Faheem Pharmacy" in msg.message_text
    gw.state = P.CONNECTED
    assert wa.process_due(db) == 1
    db.refresh(msg)
    assert msg.status == "SENT" and msg.attempt_count == 1 and msg.sent_at is not None
    phone, filename, data, mime, caption = gw.sent[0]
    assert phone == "919876543210" and mime == "application/pdf" and data.startswith(b"%PDF")
    assert filename == f"Invoice-{sale.invoice_no}.pdf" and caption == msg.message_text
    assert data == (UPLOAD_DIR / msg.pdf_path).read_bytes()


def test_resend_sends_the_same_invoice_and_creates_nothing_else(db, gw):
    sale = _sale(db)
    first = wa.send_invoice(db, sale.id)
    db.commit(); wa.process_due(db)
    again = wa.send_invoice(db, sale.id, "+91 99887 76655")
    db.commit(); wa.process_due(db)
    assert again.is_resend and again.pdf_path == first.pdf_path and db.query(Sale).count() == 1
    assert gw.sent[0][2] == gw.sent[1][2] and gw.sent[1][0] == "919988776655"


def test_temporary_failures_retry_then_fail_and_can_be_retried_by_hand(db, gw):
    sale = _sale(db)
    gw.fail = [P.TemporaryError("network down")] * 3
    msg = wa.send_invoice(db, sale.id)
    db.commit()
    wa.process_due(db); db.refresh(msg)
    assert msg.status == "QUEUED" and msg.attempt_count == 1 and msg.next_attempt_at > utcnow()   # 30 s back-off
    assert wa.process_due(db) == 0                                                               # not due yet
    _due(db); wa.process_due(db); _due(db); wa.process_due(db); db.refresh(msg)
    assert msg.status == "FAILED" and msg.attempt_count == 3 and "network down" in msg.last_error
    wa.retry(db, msg.id); db.commit(); wa.process_due(db); db.refresh(msg)
    assert msg.status == "SENT" and len(gw.sent) == 1


def test_permanent_failure_stops_at_once_and_disconnection_is_reported(db, gw):
    sale = _sale(db)
    gw.fail = [P.PermanentError("This number is not on WhatsApp")]
    msg = wa.send_invoice(db, sale.id)
    db.commit(); wa.process_due(db); db.refresh(msg)
    assert msg.status == "FAILED" and msg.attempt_count == 1
    gw.state = P.QR_REQUIRED
    other = wa.send_invoice(db, sale.id)
    db.commit(); wa.process_due(db); db.refresh(other)
    assert other.status == "QUEUED" and other.last_error == wa.DISCONNECTED_MSG


def test_invoice_image_rides_along_and_never_decides_delivery(db, gw):
    sale = _sale(db)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    wa.save_ad_image(db, "offer.png", png)
    wa.save_settings(db, {"ad_enabled": True, "ad_caption": "Free BP check every Sunday"})
    db.commit()
    wa.send_invoice(db, sale.id); db.commit(); wa.process_due(db)
    assert [s[3] for s in gw.sent] == ["application/pdf", "image/png"] and gw.sent[1][4] == "Free BP check every Sunday"
    class ImageFails(FakeGateway):
        def send_file(self, phone, filename, data, mime, caption=""):
            if mime.startswith("image/"):
                raise P.TemporaryError("image upload failed")
            return super().send_file(phone, filename, data, mime, caption)
    wa._provider_override = ImageFails()
    m = wa.send_invoice(db, sale.id); db.commit(); wa.process_due(db); db.refresh(m)
    assert m.status == "SENT"
    with pytest.raises(wa.WhatsAppError):
        wa.save_ad_image(db, "x.gif", b"GIF89a....")
    with pytest.raises(wa.WhatsAppError):
        wa.save_settings(db, {"template": "Hi {name}"})


def test_voided_bills_and_missing_numbers_are_refused(db, gw):
    sale = _sale(db, mobile=None)
    with pytest.raises(wa.WhatsAppError, match="no customer mobile"):
        wa.send_invoice(db, sale.id)
    assert wa.send_invoice(db, sale.id, "9876543210").status == "QUEUED"     # the number the customer gives
    sales_service.void_sale(db, sale, reason="wrong")
    db.commit()
    with pytest.raises(wa.WhatsAppError, match="voided"):
        wa.send_invoice(db, sale.id, "9876543210")


def _staff(db, client):
    role = db.query(Role).filter(Role.name == "Sales Staff").one()
    user_service.create_user(db, username="counter1", full_name="Counter One", role_id=role.id, password="Counter@2026",
                             must_change_password=False)
    db.commit()
    client.post("/logout")
    return login(client, "counter1", "Counter@2026")


def test_only_administrators_see_pairing_and_staff_can_send(client, db, gw):
    sale = _sale(db)
    login(client)
    gw.state = P.QR_REQUIRED
    admin = client.get("/api/erp/settings/whatsapp").json()
    assert admin["connection"]["state"] == "QR_REQUIRED" and admin["connection"]["qr"].startswith("data:image")
    _staff(db, client)
    assert client.get("/api/erp/settings/whatsapp").status_code == 403
    assert client.post("/api/erp/settings/whatsapp/logout").status_code == 403
    status = client.get("/api/erp/whatsapp/status").json()
    assert status["message"] == wa.DISCONNECTED_MSG and "qr" not in status
    assert client.post(f"/api/erp/sales/{sale.id}/whatsapp", json={}).status_code == 403   # someone else's bill: own-sales scope
    client.post("/logout")
    login(client)
    r = client.post(f"/api/erp/sales/{sale.id}/whatsapp", json={"phone": "98765 43210"})
    assert r.status_code == 200 and r.json()["message"]["whatsapp_status"] == "queued"


# --------------------------------------------------------------------------- WPPConnect client
def _wpp(handler, url="http://127.0.0.1:21465"):
    P.WPPConnect._tokens.clear()
    return P.WPPConnect(url=url, secret="s3cret", session="shop", transport=httpx.MockTransport(handler))


def test_wppconnect_requests_match_its_api():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers.get("authorization"), req.content))
        if req.url.path == "/api/shop/s3cret/generate-token":
            return httpx.Response(201, json={"status": "success", "session": "shop", "token": "t", "full": "shop:t"})
        if req.url.path == "/api/shop/status-session":
            return httpx.Response(200, json={"status": "QRCODE", "qrcode": "data:image/png;base64,AAA"})
        if req.url.path == "/api/shop/send-file-base64":
            return httpx.Response(201, json={"status": "success", "response": [{"id": "true_91987@c.us_X"}]})
        return httpx.Response(404, json={})

    gw = _wpp(handler)
    c = gw.connection()
    assert c.state == P.QR_REQUIRED and c.qr.startswith("data:image")
    assert gw.send_file("919876543210", "Invoice-1.pdf", b"%PDF-1.4", "application/pdf", "Thanks") == "true_91987@c.us_X"
    method, path, auth, body = seen[-1]
    sent = json.loads(body)
    assert (method, path, auth) == ("POST", "/api/shop/send-file-base64", "Bearer t")     # the bare token
    assert sent["phone"] == "919876543210" and sent["base64"].startswith("data:application/pdf;base64,") and sent["caption"] == "Thanks"


def test_wppconnect_errors_and_private_network_only():
    def not_on_whatsapp(req):
        if "generate-token" in req.url.path:
            return httpx.Response(201, json={"full": "shop:t"})
        return httpx.Response(400, json={"status": "error", "message": "The number does not exist"})

    with pytest.raises(P.PermanentError):
        _wpp(not_on_whatsapp).send_file("919876543210", "a.pdf", b"x", "application/pdf")
    tokens = []

    def expired(req):
        if "generate-token" in req.url.path:
            tokens.append(1)
            return httpx.Response(201, json={"full": f"shop:t{len(tokens)}"})
        if req.headers["authorization"] == "Bearer t1":
            return httpx.Response(401, json={})
        return httpx.Response(201, json={"status": "success", "response": []})

    _wpp(expired).send_file("919876543210", "a.pdf", b"x", "application/pdf")
    assert len(tokens) == 2                                     # token renewed once on 401
    public = P.WPPConnect(url="http://8.8.8.8:21465", secret="s", session="shop")
    assert "not a local or private" in public.problem() and public.connection().state == P.NOT_CONFIGURED
    assert P.WPPConnect(url="http://192.168.1.20:21465", secret="s").problem() == ""
    assert "SECRET" in P.WPPConnect(url="http://127.0.0.1:21465", secret="").problem()


def test_connect_reports_starting_while_the_gateway_boots_instead_of_failing():
    import httpx

    from app.services.whatsapp import provider as P

    def handler(request):
        if request.url.path.endswith("/generate-token"):
            return httpx.Response(201, json={"token": "t", "full": "s:t"})
        if request.url.path.endswith("/start-session"):
            raise httpx.ReadTimeout("still launching the browser", request=request)
        if request.url.path.endswith("/status-session"):
            return httpx.Response(200, json={"status": "QRCODE", "qrcode": "data:image/png;base64,AAAA"})
        return httpx.Response(404)

    gw = P.WPPConnect(url="http://127.0.0.1:21465", secret="k", session="s", transport=httpx.MockTransport(handler))
    P.WPPConnect._tokens.clear()
    assert gw.connect().state == P.STARTING                 # not an error: the page keeps polling
    c = gw.connection()
    assert c.state == P.QR_REQUIRED and c.qr.startswith("data:image/png")


def test_wppconnect_auth_as_the_real_gateway_checks_it_and_shows_a_refusal():
    import httpx

    from app.services.whatsapp import provider as P

    def gateway(req):     # wppconnect-server 2.10: Authorization "Bearer <token>"; "session:token" is refused
        if req.url.path.endswith("/generate-token"):
            return httpx.Response(201, json={"status": "success", "session": "s", "token": "HASH", "full": "s:HASH"})
        if req.headers.get("authorization") != "Bearer HASH":
            return httpx.Response(401, json={"error": "Check that the Session and Token are correct"})
        if req.url.path.endswith("/status-session"):
            return httpx.Response(200, json={"status": "QRCODE", "qrcode": "data:image/png;base64,QQ"})
        return httpx.Response(200, json={"status": "INITIALIZING"})

    P.WPPConnect._tokens.clear()
    gw = P.WPPConnect(url="http://127.0.0.1:21465", secret="k", session="s", transport=httpx.MockTransport(gateway))
    assert gw.connection().state == P.QR_REQUIRED

    def refusing(req):
        if req.url.path.endswith("/generate-token"):
            return httpx.Response(201, json={"token": "WRONG"})
        return httpx.Response(401, json={"error": "Check that the Session and Token are correct"})

    P.WPPConnect._tokens.clear()
    bad = P.WPPConnect(url="http://127.0.0.1:21465", secret="k", session="s", transport=httpx.MockTransport(refusing))
    c = bad.connection()
    assert c.state == P.ERROR and "refused" in c.detail                  # shown, not a silent "Disconnected"
