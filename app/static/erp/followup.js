// Follow-up popover — a small panel anchored where you are working (POS, a
// customer record, the Inbox). Not a page-blocking dialog: it closes with Esc
// and saves with Enter in a couple of keystrokes.
//
//   1 · 2 · 3  7 / 15 / 30 days     4  custom date (+ optional time)
//   reason, note → Enter saves      Esc closes
import { api, esc, h } from "erp/core";

const REASONS = [["REFILL", "Medicine refill"], ["AVAILABILITY", "Check availability"], ["REPEAT", "Repeat purchase"],
  ["PAYMENT", "Payment / invoice query"], ["GENERAL", "General follow-up"], ["CUSTOM", "Custom"]];

let open = null;

export function closeFollowUp() { if (open) { open.remove(); open = null; } }

/**
 * Opens the popover. mode "create" (customer + optional sale) or "reschedule" (followup).
 * Resolves with the saved follow-up payload, or null when closed.
 */
export function followUpPopover({ anchor, customer, sale = null, followup = null, ctx }) {
  closeFollowUp();
  const reschedule = !!followup;
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const el = h(`<form class="fu-pop" role="dialog" aria-label="${reschedule ? "Reschedule follow-up" : "Follow up"}" novalidate>
      <header><b>${reschedule ? "Reschedule" : "Follow up with"} ${esc(customer.name || "")}</b>${customer.mobile ? `<span class="muted">${esc(customer.mobile)}</span>` : ""}</header>
      <div class="fu-presets" role="radiogroup">
        ${[["7", "7 days"], ["15", "15 days"], ["30", "30 days"], ["custom", "Custom"]].map(([v, l], i) =>
          `<label><input type="radio" name="preset" value="${v}" ${i === 0 ? "checked" : ""}> ${l} <kbd>${i + 1}</kbd></label>`).join("")}
      </div>
      <div class="fu-custom" hidden><label>Date<input type="date" name="due_date"></label><label>Time<input type="time" name="due_time"></label></div>
      ${reschedule ? "" : `<label>Reason<select name="reason">${REASONS.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select></label>`}
      <label>Note<input name="note" maxlength="300" placeholder="${reschedule ? "why it moved (optional)" : "e.g. ask about monthly refill"}"></label>
      ${sale ? `<p class="hint">From invoice ${esc(sale.invoice_no || "")}</p>` : ""}
      <p class="fu-err" role="alert"></p>
      <footer><button type="button" class="btn" data-x>Close <kbd>Esc</kbd></button><button type="submit" class="btn primary">Save <kbd>Enter</kbd></button></footer>
    </form>`);
    const close = (value) => { el.remove(); if (open === el) open = null; if (prev && prev.focus) prev.focus(); resolve(value); };
    const custom = el.querySelector(".fu-custom");
    const sync = () => { custom.hidden = el.preset.value !== "custom"; if (!custom.hidden) el.due_date.focus(); };
    el.addEventListener("change", (e) => { if (e.target.name === "preset") sync(); });
    el.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); close(null); return; }
      const typing = e.target.tagName === "SELECT" || (e.target.tagName === "INPUT" && e.target.type !== "radio");
      if (/^[1-4]$/.test(e.key) && !typing) {
        e.preventDefault();
        const r = el.querySelectorAll("input[name=preset]")[Number(e.key) - 1];
        r.checked = true; sync();
      }
      if (e.key === "Enter" && e.target.tagName === "SELECT") { e.preventDefault(); el.requestSubmit(); }
    });
    el.querySelector("[data-x]").onclick = () => close(null);
    el.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = el.querySelector(".fu-err");
      err.textContent = "";
      const preset = el.preset.value;
      const body = { preset: preset === "custom" ? "" : preset, due_date: el.due_date.value, due_time: el.due_time.value, note: el.note.value };
      if (preset === "custom" && !el.due_date.value) { err.textContent = "Choose the date"; return; }
      try {
        const out = reschedule
          ? await api(`/api/erp/followups/${followup.id}/reschedule`, { method: "POST", body })
          : await api("/api/erp/followups", { method: "POST", body: { ...body, customer_id: customer.id, reason: el.reason.value,
            source_sale_id: sale ? sale.id : null } });
        if (ctx) ctx.status(`Follow-up ${reschedule ? "moved to" : "due"} ${out.followup.due_date.split("-").reverse().join("-")} · ${customer.name}`, "ok");
        close(out.followup);
      } catch (ex) { err.textContent = ex.message; }
    });
    document.body.append(el);
    open = el;
    const box = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: window.innerWidth / 2 - 170, bottom: 120 };
    el.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 360)) + "px";
    el.style.top = Math.max(8, Math.min(box.bottom + 6, window.innerHeight - 330)) + "px";
    el.querySelector("input[name=preset]").focus();
  });
}
