/* Invoice studio frame: renders one saved invoice inside the ERP Sales tab and
   prints itself (no pop-up window). The Sales tab drives it through
   window.studio: setSize('A4'|'A5'|'LETTER'|'THERMAL80'|'THERMAL58'),
   setMono(bool), setExpiry(bool), print(). */
(function () {
  "use strict";
  const S = window.InvoiceKitSettings, pages = document.getElementById("invoice-pages");
  const printStyle = document.getElementById("print-rules");
  const opt = window.INVOICE_OPTIONS || {};
  const state = { size: opt.size || "A4", mono: !!opt.mono, expiry: opt.expiry !== false };
  let token = 0, ready = null;

  function report(msg) { try { parent.postMessage({ source: "invoice-studio", ...msg }, location.origin); } catch (e) { /* standalone */ } }

  async function render() {
    const mine = ++token;
    try {
      const r = await window.InvoiceKit.paginate(pages, window.INVOICE_DATA,
        S.build(window.INVOICE_DATA, { size: state.size, monochrome: state.mono, expiry: state.expiry }), printStyle);
      if (mine === token) report({ type: "rendered", pages: r.pages, size: state.size, label: S.SIZE_NAMES[state.size] || state.size });
    } catch (e) {
      // never a silent blank page: say why the invoice could not be drawn
      const p = document.createElement("p");
      p.style.cssText = "font:14px system-ui,sans-serif;color:#9b1c1c;background:#fff;padding:16px 20px;border:1px solid #e3b4b4;max-width:640px";
      p.textContent = "This invoice could not be drawn: " + e.message;
      pages.replaceChildren(p);
      report({ type: "error", message: e.message });
    }
  }
  window.studio = {
    setSize(size) { state.size = size; if (size.startsWith("THERMAL")) state.mono = true; ready = render(); return state; },
    setMono(v) { state.mono = !!v; ready = render(); return state; },
    setExpiry(v) { state.expiry = !!v; ready = render(); return state; },
    state: () => ({ ...state }),
    async print() { await ready; await (document.fonts ? document.fonts.ready : Promise.resolve()); window.focus(); window.print(); },
  };
  // keys pressed while the invoice has focus belong to the workspace (tabs, formats, print…)
  document.addEventListener("keydown", (e) => {
    if (parent === window) return;
    try {
      const fwd = new parent.KeyboardEvent("keydown", { key: e.key, code: e.code, ctrlKey: e.ctrlKey, altKey: e.altKey,
        shiftKey: e.shiftKey, metaKey: e.metaKey, repeat: e.repeat, bubbles: true, cancelable: true });
      parent.document.dispatchEvent(fwd);
      if (fwd.defaultPrevented) e.preventDefault();
    } catch (err) { /* standalone */ }
  }, true);
  ready = render();
  if (opt.autoprint) ready.then(() => setTimeout(() => window.studio.print(), 250));
})();
