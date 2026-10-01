import * as keys from "erp/keys";
// Settings — administrators only. One section for now: WhatsApp Invoicing
// (pair / reconnect / log out, the invoice message, the image sent with invoices,
// a test send and the delivery activity). Gateway credentials never reach this
// screen: they live in the server's environment.
import { $, api, esc, fmtDateTime } from "erp/core";
import { WA_ICON, normalizePhone } from "erp/whatsapp";

const STATE = {
  CONNECTED: ["Connected", "ok"], DISCONNECTED: ["Disconnected", "bad"], QR_REQUIRED: ["Waiting for the phone to scan the QR code", "warn"],
  STARTING: ["Starting…", "warn"], NOT_CONFIGURED: ["Not set up", "bad"], ERROR: ["Gateway error", "bad"],
};

export function create(ctx, params, root) {
  let data = null, poll = null, previewTimer = null, connectUntil = 0;
  ctx.setTitle("Settings");
  root.innerHTML = `<div class="masters erpset">
    <div class="mtabs" role="tablist"><button type="button" class="on" role="tab" data-tab="wa">WhatsApp invoicing</button>
      <button type="button" role="tab" data-tab="store">Invoice Store</button>
      <span class="hint">Administrator only</span></div>
    <section class="set-scroll" data-panel="wa"><p class="muted es-pad">Loading…</p></section>
    <section class="set-scroll" data-panel="store" hidden><p class="muted es-pad">Loading…</p></section></div>`;
  const body = $('.set-scroll[data-panel="wa"]', root);
  const store = invoiceStore(ctx, $('.set-scroll[data-panel="store"]', root));
  let tab = "wa";
  const showTab = (t) => {
    tab = t;
    root.querySelectorAll(".mtabs [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    root.querySelectorAll(".set-scroll[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== t; });
    if (t === "store") store.load();
  };
  root.querySelector(".mtabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });
  if (params && params.tab === "store") setTimeout(() => showTab("store"), 0);

  async function load() {
    try { data = await api("/api/erp/settings/whatsapp"); render(); }
    catch (err) { body.innerHTML = `<p class="bad">${esc(err.message)}</p>`; }
  }

  const sec = (title, right, inner, foot = "") => `<section class="es">
      <header><b>${title}</b>${right ? `<span>${right}</span>` : ""}</header>
      <div class="es-body">${inner}</div>${foot ? `<footer>${foot}</footer>` : ""}</section>`;
  const row = (label, value) => `<div class="es-row"><label>${label}</label><div>${value}</div></div>`;

  function render() {
    const c = data.connection, [label, tone] = STATE[c.state] || [c.state, "bad"];
    const q = data.queue || {};
    const canLink = c.state !== "NOT_CONFIGURED";
    body.innerHTML = `
      ${sec("Connection", "",
        row("Status", `<b class="st-${tone}">${esc(label)}</b>`)
        + row("Detail", `<span>${esc(c.detail || "—")}</span>`)
        + (c.state === "QR_REQUIRED" && c.qr ? row("Link the phone", `<div class="es-qr"><img src="${esc(c.qr)}" alt="WhatsApp pairing QR code" width="220" height="220">
            <ol><li>On the pharmacy phone open WhatsApp.</li><li>Settings → Linked devices → Link a device.</li><li>Point the phone at this code. This screen updates when it is linked.</li></ol></div>`) : "")
        + (c.state === "NOT_CONFIGURED" ? row("Setup", `<span>The WhatsApp gateway is off on this PC. Turn it on in <b>ERP Control Center → Settings → WhatsApp invoices</b>
            (or <code>sudo faheem-erp whatsapp enable</code>), then press Refresh.</span>`) : "")
        + row("Session", "Survives restarts — after a reboot it reconnects by itself while the phone keeps the link."),
        `<button type="button" class="btn primary" data-act="connect" ${canLink ? "" : "disabled"}>${c.state === "CONNECTED" ? "Reconnect" : c.state === "QR_REQUIRED" ? "New QR code" : "Connect WhatsApp"}</button>
         <button type="button" class="btn" data-act="logout" ${c.state === "CONNECTED" || c.state === "QR_REQUIRED" ? "" : "disabled"}>Log out WhatsApp</button>
         <button type="button" class="btn" data-act="refresh">Refresh <kbd data-shortcut="settings.refresh">${esc(keys.keyFor("settings.refresh"))}</kbd></button>`)}

      ${sec("Invoice message", "",
        row("Message", `<textarea class="wa-template" rows="3" maxlength="1000">${esc(data.template)}</textarea>`)
        + row("Insert field", Object.entries(data.placeholders).map(([k, v]) => `<button type="button" class="es-field" data-ph="${k}" title="${esc(v)}">{${k}}</button>`).join(""))
        + row("Preview", `<div class="es-preview">…</div>`),
        `<button type="button" class="btn primary" data-act="save-template">Save message</button>
         <button type="button" class="btn" data-act="default-template">Use default</button>`)}

      ${sec("Image sent with invoices", data.ad.updated_at ? `replaced ${esc(fmtDateTime(data.ad.updated_at + "Z"))}` : "",
        row("Image", `<div class="es-img">${data.ad.has_image ? `<img src="/api/erp/settings/whatsapp/image?t=${encodeURIComponent(data.ad.updated_at)}" alt="Image sent with invoices">` : '<span class="muted">No image</span>'}</div>
            <span class="muted small">PNG / JPEG / WebP up to 2 MB, sent after the invoice only when a customer asks for it.</span>`)
        + row("Send", `<label class="es-chk"><input type="checkbox" class="ad-enabled" ${data.ad.enabled ? "checked" : ""} ${data.ad.has_image ? "" : "disabled"}> Send this image with invoices</label>`)
        + row("Caption", `<input class="ad-caption" maxlength="300" value="${esc(data.ad.caption)}" placeholder="e.g. Free BP check every Sunday">`),
        `<label class="btn">${data.ad.has_image ? "Replace image" : "Upload image"}<input type="file" class="ad-file" accept="image/png,image/jpeg,image/webp" hidden></label>
         ${data.ad.has_image ? '<button type="button" class="btn" data-act="remove-image">Remove image</button>' : ""}
         <button type="button" class="btn primary" data-act="save-ad">Save</button>`)}

      ${sec("Test", "",
        row("Mobile", `<input class="test-phone" inputmode="tel" maxlength="16" placeholder="98765 43210">
            <span class="muted small">Sends the latest bill's invoice, with the message and image above.</span>`),
        `<button type="button" class="btn" data-act="test" ${c.state === "CONNECTED" ? "" : "disabled"}>Send test</button>`)}

      ${sec("Activity", `sent ${q.sent || 0} · queued ${(q.queued || 0) + (q.sending || 0)} · failed ${q.failed || 0}`,
        data.recent.length ? `<div class="grid es-grid"><table><colgroup><col style="width:170px"><col style="width:140px"><col style="width:110px"><col style="width:150px"><col></colgroup>
          <thead><tr><th>Invoice</th><th>To</th><th>Status</th><th>When</th><th>Note</th></tr></thead><tbody>
          ${data.recent.map((m) => `<tr><td class="mono">${esc(m.invoice_no)}</td><td class="mono">${esc(m.customer_phone)}</td>
            <td class="st-${m.whatsapp_status === "sent" ? "ok" : m.whatsapp_status === "failed" ? "bad" : "warn"}">${esc(m.whatsapp_status)}${m.is_resend ? " · resend" : ""}</td>
            <td>${esc(fmtDateTime(m.sent_at || m.queued_at))}</td><td class="${m.whatsapp_status === "failed" ? "st-bad" : "muted"}">${esc(m.last_error || "")}</td></tr>`).join("")}
          </tbody></table></div>` : '<p class="muted es-pad">Nothing sent yet.</p>')}`;
    preview();
    // after Connect, keep checking for a while even through "disconnected": the session is booting
    if (c.state === "QR_REQUIRED" || c.state === "STARTING" || Date.now() < connectUntil) startPoll(); else stopPoll();
  }

  function startPoll() {
    if (poll) return;
    poll = setInterval(async () => {
      try {
        const c = await api("/api/erp/settings/whatsapp/connection");
        if (c.state !== data.connection.state || (c.qr && c.qr !== data.connection.qr) || c.detail !== data.connection.detail) {
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
        $(".es-preview", body).textContent = d.text;
      } catch (err) { $(".es-preview", body).textContent = err.message; }
    }, 250);
  }

  async function act(name) {
    try {
      if (name === "refresh") return load();
      if (name === "connect") {
        const btn = $('[data-act="connect"]', body);
        if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
        ctx.status("Starting WhatsApp — the QR code appears here in a moment…");
        connectUntil = Date.now() + 180000;
        data = await api("/api/erp/settings/whatsapp/connect", { method: "POST" }); render();
        if (data.connection.state === "ERROR") ctx.status(data.connection.detail || "WhatsApp gateway error", "error");
        return;
      }
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

  body.addEventListener("click", (e) => {
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
  body.addEventListener("input", (e) => { if (e.target.classList.contains("wa-template")) preview(); });
  body.addEventListener("change", async (e) => {
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
    onKey(e, name) { if (keys.matches("settings.refresh", name)) { if (tab === "store") store.load(); else load(); return true; } return false; },
    onShow() { if (tab === "store") store.load(); else if (data) load(); },
    onHide: stopPoll,
    destroy: stopPoll,
  };
}


// ---------------------------------------------------------------- Invoice Store (premium WhatsApp invoices)
function invoiceStore(ctx, el) {
  let v = null, page = 1, pages = 1, items = 5, previewFor = null;
  const sec = (title, right, inner, foot = "") => `<section class="es"><header><b>${title}</b>${right ? `<span>${right}</span>` : ""}</header>
    <div class="es-body">${inner}</div>${foot ? `<footer>${foot}</footer>` : ""}</section>`;
  const row = (label, value) => `<div class="es-row"><label>${label}</label><div>${value}</div></div>`;
  const field = (k, ph = "", w = 420) => `<input class="sf" data-k="${k}" value="${esc(v.fields[k] || (v.defaults[k] || ""))}" placeholder="${esc(ph)}" style="width:${w}px;max-width:100%">`;

  async function load() {
    try { v = await api("/api/erp/settings/invoice-store"); render(); }
    catch (err) { el.innerHTML = `<p class="bad es-pad">${esc(err.message)}</p>`; }
  }
  const premium = () => v.templates.some((t) => t.id === v.template && t.id !== "classic");
  function previewSrc() {
    const t = premium() ? v.template : "a4-compact-stamp";
    return `/api/erp/settings/invoice-store/preview.png?template=${t}&items=${items}&page=${page}&dpi=110&t=${Date.now()}`;
  }
  async function refreshPreview() {
    const img = el.querySelector(".is-prev img");
    if (!img) return;
    const src = previewSrc();
    previewFor = src;
    try {
      const r = await fetch(src, { credentials: "same-origin" });
      if (!r.ok) throw new Error("Preview failed");
      pages = Number(r.headers.get("X-Pages") || 1);
      if (previewFor !== src) return;
      img.src = URL.createObjectURL(await r.blob());
      el.querySelector(".is-page").textContent = `Page ${page} of ${pages}`;
    } catch (err) { ctx.status(err.message, "error"); }
  }

  function render() {
    const sw = v.switches;
    el.innerHTML = `
      ${sec("Template for WhatsApp invoices", "printing keeps the classic invoice",
        `<div class="is-grid"><div class="grid es-grid"><table><colgroup><col style="width:34px"><col style="width:250px"><col></colgroup>
          <thead><tr><th></th><th>Template</th><th>Layout</th></tr></thead><tbody>
          ${v.templates.map((t) => `<tr class="${t.id === v.template ? "sel" : ""}" data-t="${t.id}"><td><input type="radio" name="is-t" value="${t.id}" ${t.id === v.template ? "checked" : ""}></td>
            <td><b>${esc(t.name)}</b></td><td class="muted">${esc(t.description)}</td></tr>`).join("")}
          </tbody></table></div>
          <div class="is-prev"><div class="is-prev-bar"><label>Items in preview <select class="is-items">${[1, 5, 7, 12].map((n) => `<option ${n === items ? "selected" : ""}>${n}</option>`).join("")}</select></label>
            <button type="button" class="btn" data-a="prev">◀</button><span class="is-page">Page 1 of 1</span><button type="button" class="btn" data-a="next">▶</button>
            <button type="button" class="btn" data-a="pdf">Open PDF</button></div>
            <img alt="Invoice preview" width="300">${premium() ? "" : '<p class="muted small">Classic is selected — the preview shows the premium layout.</p>'}</div></div>`)}

      ${sec("Shop details", "printed on every template",
        row("Pharmacy name", field("pharmacy_name", "Faheem Pharmacy"))
        + row("Address", `<textarea class="sf" data-k="address" rows="2" maxlength="300" style="width:420px;max-width:100%">${esc(v.fields.address || "")}</textarea>`)
        + row("Contact numbers", field("contact_numbers", "98xxxxxxxx / 98xxxxxxxx"))
        + row("Email", field("pharmacy_email", "name@example.com")))}

      ${sec("Optional fields", "shown only when switched on",
        row("GSTIN", `<label class="es-chk"><input type="checkbox" class="ss" data-k="show_gst" ${sw.show_gst ? "checked" : ""}> Show</label>${field("gst_number", "15-character GSTIN", 220)}`)
        + row("Drug licence", `<label class="es-chk"><input type="checkbox" class="ss" data-k="show_drug_license" ${sw.show_drug_license ? "checked" : ""}> Show</label>${field("drug_license_number", "e.g. 20B/21B numbers", 300)}`))}

      ${sec("Invoice text", "",
        row("Thank-you line", field("invoice_store_thanks", v.defaults.invoice_store_thanks))
        + row("Note", field("invoice_store_note", v.defaults.invoice_store_note))
        + row("Closing line", field("invoice_store_closing", v.defaults.invoice_store_closing))
        + row("Items per page", `<select class="is-per">${[3, 4, 5, 6, 7, 8].map((n) => `<option ${n === v.items_per_page ? "selected" : ""}>${n}</option>`).join("")}</select>
            <span class="muted small">a line is never split across pages; the summary and stamp come on the last page</span>`),
        `<button type="button" class="btn primary" data-a="save">Save details</button>`)}

      ${sec("Stamp", v.custom_stamp ? "your uploaded stamp" : "bundled stamp",
        row("Stamp", `<div class="es-img is-stamp"><img src="/api/erp/settings/invoice-store/stamp.png?t=${Date.now()}" alt="Stamp"></div>
            <span class="muted small">SVG or PNG with a transparent background, up to 2 MB. Used by “A4 Compact — with stamp”.</span>`),
        `<label class="btn">Upload stamp<input type="file" class="is-file" accept=".svg,image/svg+xml,image/png" hidden></label>
         ${v.custom_stamp ? '<button type="button" class="btn" data-a="reset-stamp">Use the bundled stamp</button>' : ""}`)}`;
    page = Math.min(page, pages);
    refreshPreview();
  }

  async function save(body, msg) {
    try { v = await api("/api/erp/settings/invoice-store", { method: "PUT", body }); render(); ctx.status(msg, "ok"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  el.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.name === "is-t") { save({ template: t.value }, `WhatsApp invoices now use “${v.templates.find((x) => x.id === t.value).name}”`); return; }
    if (t.classList.contains("is-items")) { items = Number(t.value); page = 1; refreshPreview(); return; }
    if (t.classList.contains("is-file") && t.files.length) {
      const fd = new FormData(); fd.append("file", t.files[0]);
      try {
        const r = await fetch("/api/erp/settings/invoice-store/stamp", { method: "POST", body: fd, credentials: "same-origin" });
        const d = await r.json(); if (!r.ok) throw new Error(d.detail || "Upload failed");
        v = d; render(); ctx.status("Stamp replaced", "ok");
      } catch (err) { ctx.status(err.message, "error"); }
    }
  });
  el.addEventListener("click", async (e) => {
    const tr = e.target.closest("tr[data-t]");
    if (tr && !e.target.matches("input")) { const r = tr.querySelector("input"); if (!r.checked) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); } return; }
    const b = e.target.closest("[data-a]");
    if (!b) return;
    if (b.dataset.a === "prev") { if (page > 1) { page--; refreshPreview(); } }
    else if (b.dataset.a === "next") { if (page < pages) { page++; refreshPreview(); } }
    else if (b.dataset.a === "pdf") window.open(`/api/erp/settings/invoice-store/preview.pdf?template=${premium() ? v.template : "a4-compact-stamp"}&items=${items}`, "_blank");
    else if (b.dataset.a === "save") {
      const fields = {}, switches = {};
      el.querySelectorAll(".sf").forEach((i) => { fields[i.dataset.k] = i.value; });
      el.querySelectorAll(".ss").forEach((i) => { switches[i.dataset.k] = i.checked; });
      save({ fields, switches, items_per_page: Number(el.querySelector(".is-per").value) }, "Invoice details saved");
    } else if (b.dataset.a === "reset-stamp") {
      try { v = await api("/api/erp/settings/invoice-store/stamp", { method: "DELETE" }); render(); ctx.status("Using the bundled stamp", "ok"); }
      catch (err) { ctx.status(err.message, "error"); }
    }
  });
  return { load };
}
