import * as keys from "erp/keys";
// Settings — administrators only. One section for now: WhatsApp Invoicing
// (pair / reconnect / log out, the invoice message, the image sent with invoices,
// a test send and the delivery activity). Gateway credentials never reach this
// screen: they live in the server's environment.
import { $, api, esc, fmtDateTime } from "erp/core";
import { WA_ICON, normalizePhone } from "erp/whatsapp";

const STATE = {
  CONNECTED: ["Connected", "ok"], DISCONNECTED: ["Disconnected", "bad"], QR_REQUIRED: ["QR required", "warn"],
  STARTING: ["Starting…", "warn"], NOT_CONFIGURED: ["Not set up", "bad"], ERROR: ["Gateway not reachable", "bad"],
};

export function create(ctx, params, root) {
  let data = null, poll = null, previewTimer = null;
  ctx.setTitle("Settings");
  root.innerHTML = `<div class="settings">
    <nav class="set-nav"><h2>Settings</h2><button type="button" class="on" data-sec="whatsapp">${WA_ICON} WhatsApp Invoicing</button></nav>
    <section class="set-body"><p class="muted">Loading…</p></section></div>`;
  const body = $(".set-body", root);

  async function load() {
    try { data = await api("/api/erp/settings/whatsapp"); render(); }
    catch (err) { body.innerHTML = `<p class="bad">${esc(err.message)}</p>`; }
  }

  function render() {
    const c = data.connection, [label, tone] = STATE[c.state] || [c.state, "bad"];
    const q = data.queue || {};
    body.innerHTML = `
      <header class="set-head"><h1>${WA_ICON} WhatsApp Invoicing</h1>
        <p class="muted">Customers who ask for it get their invoice PDF on WhatsApp from the pharmacy's own number. Only invoices — never bulk or promotional messages.</p></header>

      <div class="set-card">
        <h3>Connection <span class="wa-pill ${tone}">${label}</span></h3>
        <p class="muted">${esc(c.detail || "")}</p>
        ${c.state === "QR_REQUIRED" && c.qr ? `<div class="wa-qr"><img src="${esc(c.qr)}" alt="WhatsApp pairing QR code" width="240" height="240">
          <ol><li>On the pharmacy phone open <b>WhatsApp</b>.</li><li>Tap <b>⋮ / Settings → Linked devices → Link a device</b>.</li>
          <li>Point the phone at this code. This page updates by itself when it is linked.</li></ol></div>` : ""}
        ${c.state === "NOT_CONFIGURED" ? `<div class="hint-box"><b>Set up the WhatsApp gateway on the server first</b><br>
          Open <b>ERP Control Center → Settings → WhatsApp invoices</b> on the pharmacy PC, or run
          <code>sudo faheem-erp whatsapp enable</code>. The gateway runs on this PC only and is never reachable from the network.
          Then return here to link the phone.</div>` : ""}
        <div class="set-actions">
          <button type="button" class="btn primary" data-act="connect" ${c.state === "NOT_CONFIGURED" ? "disabled" : ""}>${c.state === "CONNECTED" ? "Reconnect" : c.state === "QR_REQUIRED" ? "New QR code" : "Connect WhatsApp"}</button>
          <button type="button" class="btn danger" data-act="logout" ${c.state === "CONNECTED" || c.state === "QR_REQUIRED" ? "" : "disabled"}>Log out WhatsApp</button>
          <button type="button" class="btn" data-act="refresh">Refresh <kbd data-shortcut="settings.refresh">${esc(keys.keyFor("settings.refresh"))}</kbd></button>
        </div>
        <p class="muted small">The linked session survives restarts: after a reboot it reconnects by itself, no new QR needed while the phone keeps the link.</p>
      </div>

      <div class="set-card">
        <h3>Invoice message</h3>
        <textarea class="wa-template" rows="3" maxlength="1000">${esc(data.template)}</textarea>
        <p class="small">Insert: ${Object.entries(data.placeholders).map(([k, v]) => `<button type="button" class="chip ph" data-ph="${k}" title="${esc(v)}">{${k}}</button>`).join(" ")}</p>
        <div class="wa-preview"><span class="muted small">Preview</span><div class="wa-bubble">…</div></div>
        <div class="set-actions"><button type="button" class="btn primary" data-act="save-template">Save message</button>
          <button type="button" class="btn" data-act="default-template">Use default</button></div>
      </div>

      <div class="set-card">
        <h3>Image sent with invoices</h3>
        <p class="muted small">An offer or notice (PNG / JPEG / WebP, up to 2 MB) attached after the invoice — only when a customer asks for their invoice. Replace it any time; earlier invoices keep what they were sent with.</p>
        <div class="wa-ad">
          <div class="wa-ad-img">${data.ad.has_image ? `<img src="/api/erp/settings/whatsapp/image?t=${encodeURIComponent(data.ad.updated_at)}" alt="Image sent with invoices">` : '<span class="muted">No image</span>'}</div>
          <div class="wa-ad-form">
            <label class="chk"><input type="checkbox" class="ad-enabled" ${data.ad.enabled ? "checked" : ""} ${data.ad.has_image ? "" : "disabled"}> Send this image with invoices</label>
            <label>Caption<input class="ad-caption" maxlength="300" value="${esc(data.ad.caption)}" placeholder="e.g. Free BP check every Sunday"></label>
            <div class="set-actions">
              <label class="btn">${data.ad.has_image ? "Replace image" : "Upload image"}<input type="file" class="ad-file" accept="image/png,image/jpeg,image/webp" hidden></label>
              ${data.ad.has_image ? '<button type="button" class="btn danger" data-act="remove-image">Remove</button>' : ""}
              <button type="button" class="btn primary" data-act="save-ad">Save</button>
            </div>
            ${data.ad.updated_at ? `<p class="muted small">Image replaced ${esc(fmtDateTime(data.ad.updated_at + "Z"))}</p>` : ""}
          </div>
        </div>
      </div>

      <div class="set-card">
        <h3>Test</h3>
        <p class="muted small">Sends the latest bill's invoice (with the message and image above) to a number you choose — e.g. your own.</p>
        <div class="set-actions"><input class="test-phone" inputmode="tel" maxlength="16" placeholder="98765 43210">
          <button type="button" class="btn wa-btn" data-act="test" ${c.state === "CONNECTED" ? "" : "disabled"}>${WA_ICON} Send test</button></div>
      </div>

      <div class="set-card">
        <h3>Activity <span class="muted small">sent ${q.sent || 0} · queued ${(q.queued || 0) + (q.sending || 0)} · failed ${q.failed || 0}</span></h3>
        ${data.recent.length ? `<table class="kvtab wa-log"><thead><tr><th>Invoice</th><th>To</th><th>Status</th><th>When</th><th>Note</th></tr></thead><tbody>
          ${data.recent.map((m) => `<tr><td class="mono">${esc(m.invoice_no)}</td><td class="mono">${esc(m.customer_phone)}</td>
            <td><span class="wa-st wa-${m.whatsapp_status}">${esc(m.whatsapp_status)}</span>${m.is_resend ? ' <small class="muted">resend</small>' : ""}</td>
            <td class="muted">${esc(fmtDateTime(m.sent_at || m.queued_at))}</td><td class="small ${m.whatsapp_status === "failed" ? "bad" : "muted"}">${esc(m.last_error || "")}</td></tr>`).join("")}
          </tbody></table>` : '<p class="muted">Nothing sent yet.</p>'}
      </div>`;
    preview();
    if (c.state === "QR_REQUIRED" || c.state === "STARTING") startPoll(); else stopPoll();
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      try {
        const c = await api("/api/erp/settings/whatsapp/connection");
        if (c.state !== data.connection.state || (c.qr && c.qr !== data.connection.qr)) {
          data.connection = c; render();
          if (c.state === "CONNECTED") ctx.status("WhatsApp linked ✓ — invoices can be sent", "ok");
        }
      } catch { /* keep polling */ }
    }, 3000);
  }
  function stopPoll() { if (poll) { clearInterval(poll); poll = null; } }

  function preview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const t = $(".wa-template", body);
      if (!t) return;
      try {
        const d = await api("/api/erp/settings/whatsapp/preview", { method: "POST", body: { template: t.value } });
        $(".wa-bubble", body).textContent = d.text;
      } catch (err) { $(".wa-bubble", body).textContent = err.message; }
    }, 250);
  }

  async function act(name) {
    try {
      if (name === "refresh") return load();
      if (name === "connect") { ctx.status("Starting WhatsApp…"); data = await api("/api/erp/settings/whatsapp/connect", { method: "POST" }); render(); return; }
      if (name === "logout") {
        if (!(await window.erpConfirm("Log out WhatsApp? Invoices cannot be sent until the phone is linked again with a new QR code."))) return;
        data = await api("/api/erp/settings/whatsapp/logout", { method: "POST" }); render(); ctx.status("WhatsApp logged out", "ok"); return;
      }
      if (name === "save-template") { data = await api("/api/erp/settings/whatsapp", { method: "PUT", body: { template: $(".wa-template", body).value } }); render(); ctx.status("Invoice message saved", "ok"); return; }
      if (name === "default-template") { $(".wa-template", body).value = data.default_template; preview(); return; }
      if (name === "save-ad") {
        data = await api("/api/erp/settings/whatsapp", { method: "PUT", body: { ad_enabled: $(".ad-enabled", body).checked, ad_caption: $(".ad-caption", body).value } });
        render(); ctx.status("Invoice image settings saved", "ok"); return;
      }
      if (name === "remove-image") {
        if (!(await window.erpConfirm("Remove the image sent with invoices?"))) return;
        data = await api("/api/erp/settings/whatsapp/image", { method: "DELETE" }); render(); return;
      }
      if (name === "test") {
        const phone = normalizePhone($(".test-phone", body).value);
        if (!phone) { ctx.status("Enter a 10-digit Indian mobile number", "warn"); return; }
        const d = await api("/api/erp/settings/whatsapp/test", { method: "POST", body: { phone } });
        ctx.status(`Test: ${d.message.invoice_no} queued for ${d.message.customer_phone}`, "ok");
        setTimeout(() => load(), 5000);
      }
    } catch (err) { ctx.status(err.message, "error"); }
  }

  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (b && !b.disabled) { act(b.dataset.act); return; }
    const ph = e.target.closest("[data-ph]");
    if (ph) {
      const t = $(".wa-template", body);
      const at = t.selectionStart ?? t.value.length;
      t.value = t.value.slice(0, at) + `{${ph.dataset.ph}}` + t.value.slice(t.selectionEnd ?? at);
      t.focus(); preview();
    }
  });
  root.addEventListener("input", (e) => { if (e.target.classList.contains("wa-template")) preview(); });
  root.addEventListener("change", async (e) => {
    if (!e.target.classList.contains("ad-file") || !e.target.files.length) return;
    const fd = new FormData();
    fd.append("file", e.target.files[0]);
    try {
      const r = await fetch("/api/erp/settings/whatsapp/image", { method: "POST", body: fd, credentials: "same-origin" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Upload failed");
      data = d; render(); ctx.status("Image replaced — tick “Send this image with invoices” to use it", "ok");
    } catch (err) { ctx.status(err.message, "error"); }
  });

  load();
  return {
    get keys() { return keys.bar("settings"); },
    onKey(e, name) { if (keys.matches("settings.refresh", name)) { load(); return true; } return false; },
    onShow() { if (data) load(); },
    onHide: stopPoll,
    destroy: stopPoll,
  };
}
