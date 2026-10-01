/* Shared invoice presentation settings for the v2 engine (POS studio + history page). */
(function (root) {
  "use strict";

  const SIZE_ORDER = ["A4", "A2", "A5", "LETTER", "THERMAL80", "THERMAL58"];
  const SIZE_NAMES = {
    A4: "A4", A2: "A2", A5: "A5", LETTER: "Letter",
    THERMAL80: "80 mm receipt", THERMAL58: "58 mm receipt",
  };
  const SIZE_DIMS = {
    A4: "210 × 297 mm", A2: "420 × 594 mm", A5: "148 × 210 mm",
    LETTER: "8.5 × 11 in", THERMAL80: "80 mm roll", THERMAL58: "58 mm roll",
  };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function columns(data, opts) {
    opts = opts || {};
    const items = (data && data.items) || [];
    const has = (k) => items.some((i) => i[k]);
    return root.InvoiceKit.columns.map((c) => {
      let visible = true;
      // discount is shown once in the totals, never per line
      if (c.key === "free" || c.key === "gstPercent" || c.key === "rate" || c.key === "discountPercent") visible = false;
      else if (c.key === "hsn") visible = has("hsn");
      else if (c.key === "pack") visible = has("pack");
      else if (c.key === "manufacturer") visible = has("manufacturer");
      else if (c.key === "expiry") visible = opts.expiry !== false && has("expiry");
      const label = c.key === "batch" ? "Article ID" : c.label;
      return { ...c, label, visible };
    });
  }

  function build(data, opts) {
    opts = opts || {};
    const b = (data && data.business) || {};
    const pharmacy = [];
    if (b.address) pharmacy.push({ id: "address", label: "Address", value: b.address, visible: true });
    if (b.contact) pharmacy.push({ id: "contact", label: "Contact", value: b.contact, visible: true });
    (b.registrations || []).forEach((r, i) => pharmacy.push({ id: "reg" + i, label: r.label, value: r.value, visible: true }));
    return {
      paper: opts.size || "A4",
      marginMm: 7,
      fontPt: 8.5,
      rowPadding: 3,
      monochrome: !!opts.monochrome,
      logo: "horizontal-color.svg",
      showLogo: true,
      showName: false,
      name: b.name || "Pharmacy",
      showTitle: false,
      showCopyLabel: false,
      title: "TAX INVOICE",
      showBuyer: true,
      showInvoiceDetails: true,
      showTaxSummary: false,
      showBank: false,
      showTerms: false,
      showPayments: false,
      showSignature: false,
      signature: "For " + (b.name || "Pharmacy"),
      showItemCount: true,
      showAmountWords: true,
      showSample: false,
      pharmacyFields: pharmacy,
      buyerFields: [
        { id: "name", label: "Name", visible: true },
        { id: "address", label: "Address", visible: true },
        { id: "phone", label: "Phone", visible: true },
      ],
      invoiceFields: [
        { id: "number", label: "Invoice no.", visible: true },
        { id: "date", label: "Date", visible: true },
        { id: "cashier", label: "Served by", visible: true },
      ],
      bankFields: [],
      terms: "",
      columns: columns(data, opts),
      totalFields: root.InvoiceKit.defaults.totalFields.map((f) => ({
        ...f,
        visible: !["taxPaise", "roundingPaise", "duePaise"].includes(f.id),
      })),
    };
  }

  // Opens an isolated print window that renders the pages and prints at 100%.
  function printInvoice(data, settings, cfg) {
    cfg = cfg || {};
    const popup = window.open("", "_blank", "popup,width=1100,height=900");
    if (!popup) throw new Error("Allow the invoice print window in your browser and try again.");
    const css = cfg.css || "/static/invoice-kit-v2/invoice.css";
    const engine = cfg.engine || "/static/invoice-kit-v2/invoice-engine.js";
    const assets = cfg.assets || "/static/invoice-kit-v2/assets/";
    popup.document.open();
    popup.document.write(
      '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Invoice ' + esc(data.number) + "</title>" +
      '<link rel="stylesheet" href="' + esc(css) + '">' +
      "<style>html,body{margin:0;background:#fff}.invoice-sheet{margin:0 auto!important;box-shadow:none!important}</style>" +
      '<style id="print-rules"></style>' +
      "<script>window.INVOICE_KIT_ASSETS=" + JSON.stringify(assets) + ";</" + "script>" +
      '<script src="' + esc(engine) + '"></' + "script>" +
      '</head><body><div id="pages"></div></body></html>'
    );
    popup.document.close();
    const run = async () => {
      await (popup.document.fonts.ready || Promise.resolve());
      await popup.InvoiceKit.paginate(
        popup.document.getElementById("pages"), data, settings, popup.document.getElementById("print-rules")
      );
      popup.focus();
      popup.print();
    };
    if (popup.document.readyState === "complete") setTimeout(run, 200);
    else popup.addEventListener("load", () => setTimeout(run, 200), { once: true });
    return popup;
  }

  root.InvoiceKitSettings = { SIZE_ORDER, SIZE_NAMES, SIZE_DIMS, columns, build, printInvoice, esc };
})(globalThis);
