// No inline scripts: the CSP only allows this file.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form:not([data-live]) [data-autosubmit]").forEach((el) =>
    el.addEventListener("change", () => el.form.submit()));

  document.querySelectorAll("form[data-confirm]").forEach((form) =>
    form.addEventListener("submit", (ev) => {
      if (!confirm(form.dataset.confirm)) ev.preventDefault();
    }));

  // Connect OneDrive: go on to Microsoft by navigation, not a form redirect
  // (form-action would block that). The link works without JavaScript too.
  const signIn = document.querySelector("a[data-auto-follow]");
  if (signIn) window.location.assign(signIn.href);

  document.querySelectorAll("[data-copy]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        btn.textContent = "Copied";
      } catch (e) {
        btn.textContent = "Select the address above and copy it";
      }
    }));

  // Import page: it opens at once, then reads the folder's PDFs one at a time while saying what it is
  // doing, instead of the browser waiting on one slow request (scanned PDFs take several seconds each).
  const importForm = document.getElementById("import-form");
  // The button says what it will do: nothing to import but some to skip is "Skip 3", not "Import ticked".
  const ticked = (prefix) => importForm.querySelectorAll(`input[name^=${prefix}_]:checked`).length;
  const importLabel = () => {
    const [n, s] = [ticked("include"), ticked("skip")];
    return n && s ? `Import ${n} and skip ${s}` : s ? `Skip ${s}` : "Import ticked";
  };
  const relabel = () => {
    const button = importForm.querySelector("button[type=submit]");
    if (button && !button.disabled) button.textContent = importLabel();
  };
  // "Attach to": the other rows, by number and file name, to merge a row's PDF into another row's expense.
  const refreshAttach = () => {
    importForm.querySelectorAll("select[data-attach]").forEach((select) => {
      const own = Number(select.name.slice(7));
      const keep = select.value || select.dataset.selected || "";
      select.length = 1;
      importForm.querySelectorAll("input[name^=name_]").forEach((input) => {
        const k = Number(input.name.slice(5));
        if (k === own) return;
        const option = new Option(`${k === own - 1 ? "The row above" : "Row " + (k + 1)}: ${input.value}`, String(k));
        select.add(option);
      });
      select.value = keep;
      if (select.value !== keep) select.value = "";
    });
  };
  if (importForm) {
    refreshAttach();
    // Attaching a row merges it into the other one's expense, so it isn't imported or skipped by itself.
    importForm.addEventListener("change", (e) => {
      const m = /^attach_(\d+)$/.exec(e.target.name || "");
      if (!m) return;
      const attached = e.target.value !== "";
      ["include_", "skip_"].forEach((prefix) => {
        const box = importForm.elements[prefix + m[1]];
        if (box && attached) box.checked = false;
        if (box) box.disabled = attached;
      });
    });
  }
  if (importForm) {
    importForm.addEventListener("change", relabel);
    relabel();
    // A category that has a period (insurance, registration) shows its start and end dates.
    importForm.addEventListener("change", (e) => {
      if (!/^category_id_\d+$/.test(e.target.name || "")) return;
      const row = e.target.closest("tr");
      const fields = row.querySelector("[data-period-fields]");
      const services = row.querySelector("[data-service-fields]");
      const gst = row.querySelector("[data-gst-fields]");
      const chosen = e.target.selectedOptions[0];
      if (gst) gst.hidden = !(chosen && chosen.dataset.gstfree === "1") && !gst.querySelector("input").value;
      if (fields) fields.hidden = !(chosen && chosen.dataset.period === "1");
      if (services) services.hidden = !(chosen && chosen.dataset.service === "1");
    });
  }

  // "Add an item" on an import row: a dialog that posts to the server and adds the item to every row's list,
  // so nothing typed on the page is lost.
  const serviceDialog = document.getElementById("service-dialog");
  if (importForm && serviceDialog) {
    const dialogForm = document.getElementById("service-dialog-form");
    const dialogStatus = document.getElementById("service-dialog-status");
    let opener = null;
    importForm.addEventListener("click", (e) => {
      const button = e.target.closest("[data-add-service]");
      if (!button) return;
      opener = button.closest("[data-service-fields]");
      dialogForm.reset();
      dialogStatus.textContent = "";
      serviceDialog.showModal();
      dialogForm.elements.name.focus();
    });
    document.getElementById("service-dialog-cancel").addEventListener("click", () => serviceDialog.close());
    const addToRow = (fields, item, tick) => {
      const list = fields.querySelector("[data-service-list]");
      const wanted = item.name.toLowerCase();
      let box = [...list.querySelectorAll("input")].find((i) => i.value === String(item.id)
        || i.value.toLowerCase() === "new:" + wanted);
      if (!box) {
        const label = document.createElement("label");
        label.className = "inline";
        box = document.createElement("input");
        box.type = "checkbox";
        box.name = "service_item_" + fields.dataset.n;
        box.value = item.id;
        const text = document.createElement("small");
        text.textContent = " " + item.name;
        label.append(box, text);
        list.append(label);
      }
      if (tick) box.checked = true;
    };
    importForm.addServiceItem = addToRow;
    dialogForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      dialogStatus.textContent = "Adding…";
      try {
        const res = await fetch(serviceDialog.dataset.serviceUrl, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: dialogForm.elements.name.value }),
        });
        const item = await res.json();
        if (!res.ok) { dialogStatus.textContent = item.error || "Couldn't add it."; return; }
        importForm.querySelectorAll("[data-service-fields]").forEach((f) => addToRow(f, item, f === opener));
        serviceDialog.close();
      } catch (err) {
        dialogStatus.textContent = "Couldn't add it. Try again.";
      }
    });
  }

  // Pressing Import with files left neither ticked to import nor to skip: offer to skip them, so they
  // stop turning up. Registered first, so the ticks are made before the form is sent.
  if (importForm) importForm.addEventListener("submit", () => {
    const left = [...importForm.querySelectorAll("input[name^=include_]")]
      .map((box) => ({ box, skip: importForm.elements["skip_" + box.name.slice(8)] }))
      .filter((r) => r.skip && !r.box.checked && !r.skip.checked);
    if (left.length && confirm(
      `${left.length} file${left.length === 1 ? " isn't" : "s aren't"} ticked to import. ` +
      `Skip ${left.length === 1 ? "it" : "them"} so ${left.length === 1 ? "it stops" : "they stop"} being offered?\n\n` +
      `OK skips ${left.length === 1 ? "it" : "them"}; Cancel leaves ${left.length === 1 ? "it" : "them"} for next time.`)) {
      left.forEach((r) => { r.skip.checked = true; });
      relabel();
    }
  });
  // A new car's import with no purchase invoice ticked: ask what the car is, in a dialog, before saving. The
  // dialog's fields belong to the import form, so everything typed on the page is kept.
  const carDialog = document.getElementById("car-dialog");
  if (importForm && carDialog) {
    const purchaseTicked = () => [...importForm.querySelectorAll("input[name^=include_]:checked")].some((box) => {
      const select = importForm.elements["category_id_" + box.name.slice(8)];
      const chosen = select && select.selectedOptions[0];
      return chosen && chosen.dataset.purchase === "1";
    });
    importForm.addEventListener("submit", (e) => {
      if (purchaseTicked() || importForm.elements.new_car_name.value.trim()) return;
      e.preventDefault();
      e.stopImmediatePropagation();          // the page's own submit handler would lock the button
      carDialog.showModal();
      importForm.elements.new_car_name.focus();
    });
    document.getElementById("car-dialog-go").addEventListener("click", () => {
      if (!importForm.elements.new_car_name.value.trim()) { importForm.elements.new_car_name.focus(); return; }
      carDialog.close();
      importForm.requestSubmit();
    });
    document.getElementById("car-dialog-cancel").addEventListener("click", () => carDialog.close());
    if (carDialog.dataset.needsCar) carDialog.showModal();
  }
  if (importForm && importForm.dataset.scanUrl) runImport(importForm);

  async function runImport(f) {
    const text = document.getElementById("import-status-text");
    const bar = document.getElementById("import-progress");
    const rows = f.querySelector("tbody");
    const submit = f.querySelector("button[type=submit]");
    // Say what is happening now, and how long this step has taken once it's noticeable.
    let current = "";
    let since = Date.now();
    const render = () => {
      const seconds = Math.floor((Date.now() - since) / 1000);
      text.textContent = current + (ticking && seconds >= 3 ? ` (${seconds} s)` : "");
    };
    let ticking = true;
    const ticker = setInterval(render, 1000);
    const say = (msg) => { current = msg; since = Date.now(); render(); };
    const stop = (msg, link) => {
      ticking = false;
      clearInterval(ticker);
      bar.hidden = true;
      say(msg);
      if (link) {
        const a = document.createElement("a");
        a.href = link;
        a.textContent = " Go to the " + (f.dataset.setupLabel || "Invoice storage page");
        text.append(a);
      }
    };

    // Skipping a file means not importing it: ticking one clears the other.
    f.addEventListener("change", (e) => {
      const m = /^(include|skip)_(\d+)$/.exec(e.target.name || "");
      if (m && e.target.checked) {
        const other = f.elements[(m[1] === "skip" ? "include_" : "skip_") + m[2]];
        if (other) other.checked = false;
      }
    });

    f.addEventListener("submit", () => {
      submit.disabled = true;
      submit.textContent = ticked("include") ? "Importing…" : "Saving…";
    });

    let scan;
    try {
      const res = await fetch(f.dataset.scanUrl);
      scan = await res.json();
      if (!res.ok) return stop(scan.error || "Couldn't look in the folder.", scan.url);
    } catch (e) {
      return stop("Couldn't look in the folder. Reload the page to try again.");
    }
    if (!scan.files.length) {
      return stop(scan.listed ? "Every PDF in the folder already has an expense." : "No PDFs found in that folder.");
    }

    const total = scan.files.length;
    bar.max = total;
    bar.value = 0;
    f.hidden = false;
    for (let i = 0; i < total; i++) {
      const file = scan.files[i];
      say(`Reading ${i + 1} of ${total}: ${file.name}` +
          (file.size > 300000 ? " (large or scanned PDFs take a little longer)" : ""));
      try {
        const body = new FormData();
        body.append("n", i);
        ["id", "name", "folder", "url", "size"].forEach((k) => body.append(k, file[k]));
        const res = await fetch(f.dataset.rowUrl, { method: "POST", body });
        if (!res.ok) throw new Error("HTTP " + res.status);
        rows.insertAdjacentHTML("beforeend", await res.text());
      } catch (e) {
        const tr = rows.insertRow();
        const td = tr.insertCell();
        td.colSpan = 9;
        td.textContent = `Couldn't read ${file.name} (${e.message}). Reload the page to try again.`;
      }
      bar.value = i + 1;
    }
    f.elements.count.value = total;
    refreshAttach();
    submit.disabled = false;
    f.dispatchEvent(new Event("change"));
    ticking = false;
    clearInterval(ticker);
    bar.hidden = true;
    say(`Read ${total} PDF${total === 1 ? "" : "s"}. Check each row, then press Import.`);
  }

  // Expense list: rows (a table) or tiles (a card each). view.js has already applied the saved choice
  // (phones get tiles until one is made); this wires the buttons. Filtering and sorting swap the list in
  // place instead of reloading the page, so nothing flashes and the scroll position stays put.
  const root = document.documentElement;
  const markView = () => document.querySelectorAll("[data-view-choice]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.viewChoice === root.dataset.view)));
  function bindList() {
    markView();
    document.querySelectorAll("[data-view-choice]").forEach((b) =>
      b.addEventListener("click", () => {
        root.dataset.view = b.dataset.viewChoice;
        try { localStorage.setItem("expenses-view", root.dataset.view); } catch (e) { /* not remembered */ }
        markView();
      }));
    bindColumns();
    const live = document.querySelector("form[data-live]");
    if (!live) return;
    live.querySelectorAll("[data-autosubmit]").forEach((el) =>
      el.addEventListener("change", (ev) => {
        // Blank fields (no car, no dates) stay out of the address, so plain views keep plain links.
        const params = new URLSearchParams([...new FormData(live)].filter(([, value]) => value !== ""));
        showList(location.pathname + "?" + params, el.name);
      }));
    document.querySelectorAll("a.sort").forEach((a) =>
      a.addEventListener("click", (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return;
        ev.preventDefault();
        showList(a.href, null);
      }));
  }
  // Rows: drag the edge of a column heading (or focus it and use the arrow keys) to change that column's
  // width; double-click it to go back to the built-in widths. The other columns keep theirs, so a wider
  // column makes the table wider. The widths are remembered in the browser, like the layout.
  function bindColumns() {
    const cols = window.expenseColumns;
    const table = document.querySelector("table.expense-table");
    if (!cols || !table) return;
    const heads = [...table.querySelectorAll("thead th")];
    // The columns this list has, by the class of each <col> (the Claims page's has one more than the All page's).
    const names = [...table.querySelectorAll("colgroup col")].map((c) => c.className.replace(/^c-/, ""));
    if (heads.length !== names.length || names.some((n) => !cols.names.includes(n))) return;
    const measured = () => Object.fromEntries(heads.map((th, i) => [names[i], Math.round(th.getBoundingClientRect().width)]));
    const keep = (widths) => {
      try { localStorage.setItem("expenses-columns", JSON.stringify(widths)); } catch (e) { /* not remembered */ }
    };
    heads.slice(0, -1).forEach((th, i) => {
      const name = names[i];
      const handle = document.createElement("span");
      handle.className = "col-resize";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", "Width of the " + (th.textContent.trim().replace(/\s*[▲▼]$/, "") || "last") + " column");
      handle.title = "Drag to change the column's width. Double-click for the usual widths.";
      const set = (widths, value) => { widths[name] = Math.max(56, Math.min(1200, Math.round(value))); cols.apply(widths); };
      handle.addEventListener("pointerdown", (ev) => {
        if (ev.button) return;
        ev.preventDefault();
        const widths = measured();
        const from = widths[name];
        const x0 = ev.clientX;
        handle.setPointerCapture(ev.pointerId);
        handle.classList.add("dragging");
        const move = (e) => set(widths, from + e.clientX - x0);
        const done = () => {
          handle.removeEventListener("pointermove", move);
          handle.classList.remove("dragging");
          keep(widths);
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", done, { once: true });
        handle.addEventListener("pointercancel", done, { once: true });
      });
      handle.addEventListener("keydown", (ev) => {
        if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
        ev.preventDefault();
        const widths = measured();
        set(widths, widths[name] + (ev.key === "ArrowRight" ? 16 : -16));
        keep(widths);
      });
      handle.addEventListener("dblclick", () => {
        try { localStorage.removeItem("expenses-columns"); } catch (e) { /* nothing kept */ }
        cols.apply(null);
      });
      th.append(handle);
    });
  }
  let listRequest = 0;
  async function showList(url, focusName, push = true) {
    const mine = ++listRequest;
    try {
      const res = await fetch(url, { headers: { Accept: "text/html" } });
      if (!res.ok) throw new Error(res.status);
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      // Asked for something newer meanwhile (Back then Forward, two quick changes): that answer is the
      // one to show, whichever arrives first.
      if (mine !== listRequest) return;
      const fresh = doc.querySelector("main");
      if (!fresh) throw new Error("no list");
      // The table scrolls sideways on a phone: keep it where it was, rather than snapping back to the left.
      const sideways = document.querySelector(".table-wrap")?.scrollLeft || 0;
      document.querySelector("main").replaceWith(fresh);
      const wrap = document.querySelector(".table-wrap");
      if (wrap) wrap.scrollLeft = sideways;
      if (push) history.pushState(null, "", url);
      bindList();
      const again = focusName && document.querySelector(`.filter [name=${focusName}]`);
      if (again) again.focus({ preventScroll: true });
    } catch (e) {
      window.location.assign(url);               // anything unexpected: an ordinary page load
    }
  }
  window.addEventListener("popstate", () => { if (document.querySelector("form[data-live]")) showList(location.href, null, false); });
  if (document.querySelector("form[data-live]")) bindList();

  // Add or edit an expense: "Paid from" matters only for a car with a lease, so it follows the chosen car
  // (the server draws it the same way; a choice already made stays visible so it can be undone).
  const paidFrom = document.getElementById("paid-from-field");
  if (paidFrom && paidFrom.closest("form").elements.car_id) {
    const f = paidFrom.closest("form");
    const showPaidFrom = () => {
      const option = f.elements.car_id.selectedOptions && f.elements.car_id.selectedOptions[0];
      paidFrom.hidden = !(option && option.dataset.leased) && f.elements.packaged.value === "auto";
    };
    f.elements.car_id.addEventListener("change", showPaidFrom);
  }

  // Add a reminder: a standard reminder fills in the title and how often it repeats, and the first due date
  // and odometer reading from today and the car's odometer. A car's type decides which are offered: a battery
  // electric car has no engine oil or air filter to change.
  const standardBox = document.getElementById("standard-reminder");
  if (standardBox) {
    const pick = standardBox.querySelector("select");
    const f = pick.form;
    const carOption = () => (f.elements.car_id && f.elements.car_id.selectedOptions ? f.elements.car_id.selectedOptions[0] : null);
    const offer = () => {
      const kind = (carOption() && carOption().dataset.kind) || "";
      [...pick.options].forEach((o) => {
        const kinds = (o.dataset.kinds || "").split(" ").filter(Boolean);
        o.hidden = o.disabled = Boolean(kinds.length && kind && !kinds.includes(kind));
      });
      if (pick.selectedOptions[0] && pick.selectedOptions[0].disabled) pick.value = "";
    };
    const addMonths = (iso, months) => {
      const [y, m, d] = iso.split("-").map(Number);
      const last = new Date(Date.UTC(y, m - 1 + months + 1, 0)).getUTCDate();
      return new Date(Date.UTC(y, m - 1 + months, Math.min(d, last))).toISOString().slice(0, 10);
    };
    standardBox.hidden = false;
    offer();
    if (f.elements.car_id) f.elements.car_id.addEventListener("change", offer);
    pick.addEventListener("change", () => {
      const o = pick.selectedOptions[0];
      if (!o || !o.value) return;
      const odometer = parseFloat((carOption() && carOption().dataset.odometer) || "");
      f.elements.title.value = o.dataset.title;
      f.elements.every_months.value = o.dataset.months;
      f.elements.every_km.value = o.dataset.km;
      f.elements.due_date.value = o.dataset.months ? addMonths(standardBox.dataset.today, Number(o.dataset.months)) : "";
      f.elements.due_km.value = o.dataset.km && Number.isFinite(odometer) ? String(Math.round(odometer + Number(o.dataset.km))) : "";
      const category = [...f.elements.category_id.options].find((c) => c.textContent.trim() === o.dataset.category);
      f.elements.category_id.value = category && o.dataset.category ? category.value : "";
      if (f.elements.service_item_id) {
        const item = [...f.elements.service_item_id.options].find((c) => c.textContent.trim() === o.dataset.item);
        f.elements.service_item_id.value = item && o.dataset.item ? item.value : "";
      }
    });
  }

  // Add or edit a lease. The provider's lease form fills in the boxes (never one already typed in), the lease
  // end follows the start and the term, and an associate lease hides what it has none of.
  const leaseForm = document.getElementById("lease-form");
  if (leaseForm) {
    const el = leaseForm.elements;
    const touched = new Set();
    leaseForm.addEventListener("input", (ev) => { if (ev.isTrusted && ev.target.name) touched.add(ev.target.name); });
    leaseForm.addEventListener("change", (ev) => { if (ev.isTrusted && ev.target.name) touched.add(ev.target.name); });

    const showAssociate = () => {
      leaseForm.querySelectorAll("[data-not-associate]").forEach((box) => { box.hidden = el.associate_lease.checked; });
      leaseForm.querySelectorAll("[data-associate]").forEach((box) => { box.hidden = !el.associate_lease.checked; });
    };
    el.associate_lease.addEventListener("change", showAssociate);
    showAssociate();

    // The last day of a lease: the day before the same day of the month, that many months on.
    const endFor = (iso, months) => {
      const [y, m, d] = iso.split("-").map(Number);
      const last = new Date(Date.UTC(y, m - 1 + months + 1, 0)).getUTCDate();
      const end = new Date(Date.UTC(y, m - 1 + months, Math.min(d, last)));
      end.setUTCDate(end.getUTCDate() - 1);
      return end.toISOString().slice(0, 10);
    };
    let endFilled = el.end_date.value === "";       // an end date that was typed or saved is never replaced
    const fillEnd = () => {
      const months = parseInt(el.term_months.value, 10);
      if (!endFilled || !el.start_date.value || !(months >= 1 && months <= 120)) return;
      el.end_date.value = endFor(el.start_date.value, months);
    };
    el.end_date.addEventListener("input", () => { endFilled = el.end_date.value === ""; });
    el.start_date.addEventListener("change", fillEnd);
    el.term_months.addEventListener("input", fillEnd);

    const box = (doc) => leaseForm.querySelector(`[data-lease-document=${doc}]`);
    const say = (doc, text) => { box(doc).querySelector("[data-doc-hint]").textContent = text; };
    const labels = { associate_lease: "associate lease", package_name: "provider", start_date: "start", end_date: "end",
      term_months: "term", pay_frequency: "pay frequency", pretax_payment: "payment", fbt_base_value: "FBT base value",
      ev_exempt: "electric car exemption", estimated_km_per_year: "km per year" };
    async function prefill(body) {
      say("form", "Reading the lease form…");
      try {
        const res = await fetch(leaseForm.dataset.prefillUrl, { method: "POST", body });
        const data = await res.json();
        if (!res.ok) return say("form", data.error || "Couldn't read that file.");
        const filled = [];
        Object.entries(data.fields).forEach(([name, value]) => {
          const input = el[name];
          if (!input || touched.has(name)) return;
          if (input.type === "checkbox") input.checked = Boolean(value);
          else input.value = value;
          filled.push(labels[name] || (name.startsWith("budget_") ? null : name));
        });
        if ("end_date" in data.fields) endFilled = false;
        showAssociate();
        fillEnd();
        const named = [...new Set(filled.filter(Boolean))];
        const budgets = filled.length - filled.filter(Boolean).length;
        say("form", filled.length
          ? "Filled from the lease form: " + named.concat(budgets ? ["yearly budgets"] : []).join(", ") + ". Check them before saving."
          : "Nothing could be read from that PDF as a lease form; fill the boxes in.");
      } catch (e) {
        say("form", "Couldn't read the lease form.");
      }
    }
    el.form_file.addEventListener("change", () => {
      if (!el.form_file.files.length) return;
      const body = new FormData();
      body.append("form_file", el.form_file.files[0]);
      prefill(body);
    });

    // A PDF already in the car's storage: it isn't copied, the lease keeps its link (or its path in the folder).
    const picker = document.getElementById("lease-picker");
    if (picker && leaseForm.dataset.browseUrl) {
      const status = document.getElementById("lease-picker-status");
      const pathBox = document.getElementById("lease-picker-path");
      const list = document.getElementById("lease-picker-list");
      const kind = leaseForm.dataset.storage;
      let wanted = null;
      let loading = 0;
      const entry = (label, onClick) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.className = "linklike";
        b.textContent = label;
        b.addEventListener("click", onClick);
        li.append(b);
        list.append(li);
      };
      async function show(folder) {
        const ticket = ++loading;
        status.textContent = "Loading…";
        list.textContent = "";
        let data;
        try {
          const res = await fetch(leaseForm.dataset.browseUrl + (folder ? "?folder=" + encodeURIComponent(folder) : ""));
          data = await res.json();
          if (!res.ok) throw new Error(data.error || "Couldn't open the folder.");
        } catch (e) {
          if (ticket === loading) status.textContent = e.message;
          return;
        }
        if (ticket !== loading) return;
        pathBox.textContent = data.path;
        status.textContent = data.folders.length || data.files.length ? "" : "No folders or PDFs in here.";
        if (!data.is_root) entry("⬑ Up", () => show(data.parent_id || "root"));
        data.folders.forEach((x) => entry("📁 " + x.name, () => show(x.id)));
        data.files.forEach((x) => entry("📄 " + x.name, () => choose(x)));
      }
      function choose(file) {
        picker.close();
        const doc = wanted;
        el[doc + "_file"].value = "";
        el[doc + "_url"].value = kind === "local" ? "" : file.web_url;
        el[doc + "_path"].value = kind === "local" ? file.id : "";
        el["remove_" + doc].checked = false;
        const link = box(doc).querySelector("[data-doc-link]");
        link.textContent = file.name;
        link.href = kind === "local" ? leaseForm.dataset.fileUrl + "?path=" + encodeURIComponent(file.id) : file.web_url;
        box(doc).querySelector("[data-doc-kept]").hidden = false;
        if (doc === "form") {
          const body = new FormData();
          body.append(kind + "_id", file.id);
          body.append("name", file.name);
          prefill(body);
        }
      }
      leaseForm.querySelectorAll("[data-pick]").forEach((button) => button.addEventListener("click", () => {
        wanted = button.dataset.pick;
        picker.showModal();
        show(null);
      }));
      document.getElementById("lease-picker-close").addEventListener("click", () => picker.close());
    }

    // An upload can take a few seconds: say so, and stop a second click sending it twice.
    leaseForm.addEventListener("submit", () => {
      if (!el.form_file.files.length && !el.agreement_file.files.length) return;
      const save = leaseForm.querySelector("button[type=submit]");
      save.disabled = true;
      save.textContent = "Uploading…";
    });
  }

  const form = document.getElementById("expense-form");
  if (!form) return;

  // Amount: type a sum such as 120.50+89.95 or 400/3 (or use the keys under it) and it is worked out when
  // you leave the field, press Enter, or save. Numbers, + - * / x ÷ × and brackets only: nothing is ever
  // evaluated as code. The server still checks the final figure.
  const amountInput = form.elements.amount;
  const calcNote = document.getElementById("amount-calc");
  function calculate(text) {
    const src = text.replace(/[$,\s]/g, "").replace(/[×x]/gi, "*").replace(/÷/g, "/").replace(/[−–]/g, "-");
    let pos = 0;
    const fail = () => { throw new Error("bad sum"); };
    const number = () => {
      const m = /^\d*\.?\d+|^\d+\.?/.exec(src.slice(pos));
      if (!m) fail();
      pos += m[0].length;
      return parseFloat(m[0]);
    };
    const factor = () => {
      if (src[pos] === "-") { pos++; return -factor(); }
      if (src[pos] === "(") {
        pos++;
        const v = expr();
        if (src[pos++] !== ")") fail();
        return v;
      }
      return number();
    };
    const term = () => {
      let v = factor();
      while (src[pos] === "*" || src[pos] === "/") v = src[pos++] === "*" ? v * factor() : v / factor();
      return v;
    };
    const expr = () => {
      let v = term();
      while (src[pos] === "+" || src[pos] === "-") v = src[pos++] === "+" ? v + term() : v - term();
      return v;
    };
    try {
      const v = expr();
      if (pos !== src.length || !Number.isFinite(v)) return null;
      return Math.round((v + Math.sign(v) * 1e-9) * 100) / 100;
    } catch (e) {
      return null;
    }
  }
  const isSum = (text) => /[-+*/×÷()]|\dx\d/i.test(text.replace(/^\s*[$-]?\s*/, ""));
  function settleAmount() {
    if (!isSum(amountInput.value)) return;
    const v = calculate(amountInput.value);
    if (v === null) return;                    // left as typed: the server explains what's wrong
    amountInput.value = v.toFixed(2);
    calcNote.textContent = "";
  }
  function previewAmount() {
    const v = isSum(amountInput.value) ? calculate(amountInput.value) : null;
    calcNote.textContent = v === null ? "" : `= $${v.toLocaleString("en-AU", { minimumFractionDigits: 2 })}`;
  }
  if (amountInput && calcNote) {
    amountInput.addEventListener("input", previewAmount);
    amountInput.addEventListener("blur", settleAmount);
    amountInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && isSum(amountInput.value) && calculate(amountInput.value) !== null) {
        ev.preventDefault();                   // show the answer first; Enter again saves
        settleAmount();
      }
    });
    form.addEventListener("submit", settleAmount);   // registered before the duplicate check, so it sees the figure
    const keys = document.getElementById("amount-keys");
    keys.hidden = false;
    keys.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.key === "=") {
        settleAmount();
      } else {
        const [from, to] = [amountInput.selectionStart ?? amountInput.value.length, amountInput.selectionEnd ?? amountInput.value.length];
        amountInput.setRangeText(b.dataset.key, from, to, "end");
        previewAmount();
      }
      amountInput.focus();
    }));
  }

  // Payment calculator: the true cost of a payment made of parts, such as a discounted gift card plus cash.
  // Works in whole cents so nothing drifts. The parts are never named, never sent and never saved: "Use this
  // amount" only puts the result in the amount field, and the server checks it like any typed figure.
  const payCalc = document.getElementById("pay-calc");
  if (payCalc && amountInput) {
    const rowsBox = document.getElementById("pay-rows");
    const template = document.getElementById("pay-row-template");
    const useBtn = document.getElementById("pay-use");
    const problem = document.getElementById("pay-problem");
    const money = (cents) => `${cents < 0 ? "-" : ""}$${(Math.abs(cents) / 100).toLocaleString("en-AU",
      { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    // "12.5" -> 1250 (hundredths of a unit), or null for anything that is not a plain non-negative number.
    const hundredths = (text) => {
      const m = /^(\d*)(?:\.(\d{0,2}))?$/.exec(text.replace(/[$,%\s]/g, ""));
      if (!m || (m[1] === "" && !m[2])) return null;
      return parseInt(m[1] || "0", 10) * 100 + parseInt((m[2] || "").padEnd(2, "0"), 10);
    };
    let costCents = null;

    function renumber() {
      rowsBox.querySelectorAll(".pay-row").forEach((row, i) => {
        row.querySelector(".pay-legend").textContent = `Part ${i + 1}`;
        row.querySelector("[data-part=remove]").setAttribute("aria-label", `Remove part ${i + 1}`);
      });
    }
    function addRow() {
      const row = template.content.firstElementChild.cloneNode(true);
      rowsBox.append(row);
      renumber();
      return row;
    }
    function recalc() {
      let covered = 0, cost = 0, bad = 0, used = 0;
      rowsBox.querySelectorAll(".pay-row").forEach((row) => {
        const face = row.querySelector("[data-part=face]");
        const mode = row.querySelector("[data-part=mode]").value;
        const value = row.querySelector("[data-part=value]");
        row.querySelector("[data-part-value]").hidden = mode === "cash";
        row.querySelector("[data-part-value-label]").textContent = mode === "off" ? "Percentage off" : "Amount paid";
        row.querySelector("[data-part-unit]").textContent = mode === "off" ? "%" : "$";
        const empty = face.value.trim() === "" && (mode === "cash" || value.value.trim() === "");
        const f = hundredths(face.value);
        const v = mode === "cash" ? 0 : hundredths(value.value);
        let paid = null;
        if (!empty && f !== null && v !== null) {
          if (mode === "cash") paid = f;
          else if (mode === "paid") paid = v;
          else if (v <= 10000) paid = Math.round(f * (10000 - v) / 10000);
        }
        face.setAttribute("aria-invalid", String(!empty && f === null));
        value.setAttribute("aria-invalid", String(!empty && mode !== "cash" && (v === null || (mode === "off" && v > 10000))));
        if (empty) return;
        if (paid === null) { bad++; return; }
        covered += f; cost += paid; used++;
      });
      document.getElementById("pay-covered").textContent = money(covered);
      document.getElementById("pay-cost").textContent = money(cost);
      document.getElementById("pay-saved").textContent = money(covered - cost);
      problem.textContent = bad ? "Check the parts outlined: each needs a face value, and an amount paid or a percentage from 0 to 100." : "";
      costCents = bad || !used ? null : cost;
      useBtn.disabled = costCents === null;
    }

    payCalc.hidden = false;
    addRow(); addRow();
    document.getElementById("pay-add").addEventListener("click", () => {
      addRow().querySelector("[data-part=face]").focus();
      recalc();
    });
    rowsBox.addEventListener("input", recalc);
    rowsBox.addEventListener("change", recalc);
    rowsBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-part=remove]");
      if (!btn) return;
      const row = btn.closest(".pay-row");
      const next = row.nextElementSibling || row.previousElementSibling;
      rowsBox.removeChild(row);
      if (!rowsBox.children.length) addRow();
      renumber();
      recalc();
      (next || rowsBox.firstElementChild).querySelector("[data-part=face]").focus();
    });
    rowsBox.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && ev.target.matches("input")) {   // Enter must not save the whole form
        ev.preventDefault();
        if (costCents !== null) useBtn.click();
      }
    });
    useBtn.addEventListener("click", () => {
      if (costCents === null) return;
      amountInput.value = (costCents / 100).toFixed(2);
      calcNote.textContent = "";
      amountInput.dispatchEvent(new Event("input", { bubbles: true }));
      amountInput.focus();
    });
    recalc();
  }

  // An upload can take a few seconds: say so, and stop a second click sending it twice.
  function markSending() {
    const file = form.elements.invoice_file;
    const save = form.querySelector("button[type=submit]");
    if (file && file.files.length) {
      save.disabled = true;
      save.textContent = "Uploading…";
    }
  }

  // Adding: ask the server whether this looks like an expense already recorded (same car and amount,
  // close dates) before anything is sent, so a chosen PDF isn't lost and nothing is uploaded twice.
  // The server checks again, so this only makes the question smoother.
  const dupBox = document.getElementById("duplicate-warning");
  let confirmed = false;
  function sendNow() {
    confirmed = true;
    markSending();
    form.submit();
  }
  form.addEventListener("submit", async (ev) => {
    if (!form.dataset.duplicatesUrl || confirmed || form.elements.confirm_duplicate.value) {
      markSending();
      return;
    }
    ev.preventDefault();
    const { car_id, date, time, amount } = form.elements;
    let matches = [];
    try {
      const q = new URLSearchParams({ car_id: car_id.value, date: date.value, time: time.value, amount: amount.value });
      const res = await fetch(form.dataset.duplicatesUrl + "?" + q);
      if (res.ok) matches = (await res.json()).matches;
    } catch (e) { /* the server will ask if it matters */ }
    if (!matches.length) return sendNow();
    const list = document.getElementById("duplicate-list");
    list.textContent = "";
    matches.forEach((m) => {
      const li = document.createElement("li");
      li.append([m.date, m.category, m.vendor, m.amount_text].filter(Boolean).join(" · ") + " — ");
      const a = document.createElement("a");
      a.href = m.edit_url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = "open it";
      li.append(a);
      list.append(li);
    });
    dupBox.hidden = false;
    dupBox.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  const dupSave = document.getElementById("duplicate-save");   // only in the inline version of the warning
  const dupCancel = document.getElementById("duplicate-cancel");
  if (dupSave) {
    dupSave.addEventListener("click", () => {
      form.elements.confirm_duplicate.value = "1";      // tells the server too: it checks again
      sendNow();
    });
  }
  if (dupCancel) dupCancel.addEventListener("click", () => { dupBox.hidden = true; });
  const odo = form.elements.odometer_km;
  const hint = document.getElementById("odometer-hint");

  async function fill(manual) {
    const { car_id, date, time } = form.elements;
    if (!car_id.value || !date.value || !time.value) {
      if (manual) hint.textContent = "Pick the car, date and time first.";
      return;
    }
    const q = new URLSearchParams({ car_id: car_id.value, date: date.value, time: time.value });
    try {
      const res = await fetch(form.dataset.odometerUrl + "?" + q);
      const data = await res.json();
      if (data.odometer_km == null) {
        hint.textContent = "TeslaMate has no position at or before that time.";
      } else {
        odo.value = data.odometer_km;
        hint.textContent = "Filled from TeslaMate.";
      }
    } catch (e) {
      hint.textContent = "Lookup failed.";
    }
  }

  document.getElementById("fill-odometer").addEventListener("click", () => fill(true));

  // Choosing the invoice PDF fills in what can be read from it, but never a field the person
  // has already typed in or changed.
  const pdfInput = form.elements.invoice_file;
  const prefillHint = document.getElementById("prefill-hint");
  const touched = new Set();
  ["car_id", "category_id", "date", "time", "amount", "odometer_km", "vendor", "notes", "period_start", "period_end", "gst_amount", "card_payment_ref", "bpay_biller_code", "bpay_ref"].forEach((name) => {
    const el = form.elements[name];
    if (!el) return;
    ["input", "change"].forEach((ev) => el.addEventListener(ev, () => touched.add(name)));
  });
  // The period fields follow the category: only insurance, registration and the like have one.
  const periodFields = document.getElementById("period-fields");
  const periodShown = () => {
    const chosen = form.elements.category_id.selectedOptions[0];
    periodFields.hidden = !((chosen && chosen.dataset.period === "1") || form.elements.period_start.value || form.elements.period_end.value);
  };
  if (periodFields) form.elements.category_id.addEventListener("change", periodShown);
  const gstField = document.getElementById("gst-field");
  if (gstField) form.elements.category_id.addEventListener("change", () => {
    const chosen = form.elements.category_id.selectedOptions[0];
    gstField.hidden = !(chosen && chosen.dataset.gstfree === "1") && !form.elements.gst_amount.value;
  });
  const serviceFields = document.getElementById("service-fields");
  if (serviceFields) form.elements.category_id.addEventListener("change", () => {
    const chosen = form.elements.category_id.selectedOptions[0];
    serviceFields.hidden = !(chosen && chosen.dataset.service === "1");
  });
  // Only what applies to the chosen car is offered (the server checks it too): plug-in cars for "electric", cars with an
  // engine for "combustion". A choice that no longer applies is cleared.
  const carSelect = form.elements.car_id;
  const kindFits = (applies, kind) => !kind || !applies || applies === "all" ||
    (applies === "electric" ? ["BEV", "PHEV"] : ["ICE", "PHEV", "HEV"]).includes(kind);
  const showForCar = () => {
    const kind = carSelect.selectedOptions[0] && carSelect.selectedOptions[0].dataset.kind;
    form.elements.category_id.querySelectorAll("option[data-applies]").forEach((o) => {
      const fits = kindFits(o.dataset.applies, kind);
      o.hidden = o.disabled = !fits;
      if (!fits && o.selected) {
        form.elements.category_id.value = "";
        form.elements.category_id.dispatchEvent(new Event("change"));
      }
    });
    form.querySelectorAll("#service-fields label[data-applies]").forEach((label) => {
      const fits = kindFits(label.dataset.applies, kind);
      label.hidden = !fits;
      if (!fits) label.querySelector("input").checked = false;
    });
  };
  if (carSelect && carSelect.tagName === "SELECT") carSelect.addEventListener("change", showForCar);
  // Fill the form from what /api/prefill read in a PDF; `body` is the form data to send it.
  async function prefillFrom(body) {
    prefillHint.textContent = "Reading the invoice…";
    try {
      const res = await fetch(form.dataset.prefillUrl, { method: "POST", body });
      const data = await res.json();
      if (!res.ok) {
        prefillHint.textContent = data.error || "Couldn't read that file.";
        return;
      }
      const filled = [];
      const set = (name, label, value) => {
        if (value == null || value === "" || touched.has(name)) return;
        form.elements[name].value = value;
        filled.push(label);
      };
      set("category_id", "category", data.category_id);
      set("date", "date", data.date);
      set("amount", "amount", data.amount);
      set("vendor", "vendor", data.vendor);
      set("odometer_km", "odometer", data.odometer_km);
      set("notes", "notes", data.notes);
      set("gst_amount", "GST", data.gst_amount);
      set("card_payment_ref", "payment reference", data.card_payment_ref);
      set("bpay_biller_code", "BPAY biller code", data.bpay_biller_code);
      set("bpay_ref", "BPAY reference", data.bpay_ref);
      set("period_start", "period start", data.period_start);
      set("period_end", "period end", data.period_end);
      if (periodFields) periodShown();
      if (gstField && form.elements.gst_amount.value) gstField.hidden = false;
      if (data.odometer_km && filled.includes("odometer")) hint.textContent = "Read from the invoice.";
      else if (!data.odometer_km && !touched.has("odometer_km")) await fill(false);
      prefillHint.textContent = filled.length
        ? "Filled from the invoice: " + filled.join(", ") + ". Check them before saving."
        : "Couldn't read anything from that PDF; fill the fields in.";
    } catch (e) {
      prefillHint.textContent = "Couldn't read the invoice.";
    }
  }
  if (pdfInput && prefillHint && form.dataset.prefillUrl) {
    pdfInput.addEventListener("change", () => {
      if (!pdfInput.files.length) return;
      const body = new FormData();
      body.append("invoice_file", pdfInput.files[0]);
      prefillFrom(body);
    });
  }

  // Choose a PDF that is already in the car's storage (OneDrive, or the invoice folder on the server):
  // a window over the form, so there's no page to come back from. It opens in the car's folder and can
  // go up from there. The file isn't copied: it becomes the expense's invoice (its link, or its path in
  // the folder), and what can be read in it fills the form as a chosen file would.
  const picker = document.getElementById("onedrive-picker");
  const pickButton = document.getElementById("onedrive-pick");
  const browseUrls = { onedrive: form.dataset.onedriveBrowseUrl, local: form.dataset.localBrowseUrl };
  if (picker && pickButton && (browseUrls.onedrive || browseUrls.local)) {
    // The storage the chosen car's folder is in; a car without one gets the installation's own.
    const storageOf = () => {
      const option = form.elements.car_id.selectedOptions && form.elements.car_id.selectedOptions[0];
      const own = option && option.dataset.storage;
      if (browseUrls[own]) return own;
      if (browseUrls[form.dataset.defaultStorage]) return form.dataset.defaultStorage;
      return browseUrls.onedrive ? "onedrive" : "local";
    };
    const showStorage = () => document.querySelectorAll("[data-storage-label]").forEach((el) => {
      el.hidden = el.dataset.storageLabel !== storageOf();
    });
    showStorage();
    const pickHint = document.getElementById("onedrive-pick-hint");
    const status = document.getElementById("onedrive-picker-status");
    const pathBox = document.getElementById("onedrive-picker-path");
    const list = document.getElementById("onedrive-picker-list");
    let loading = 0;

    const entry = (label, onClick) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "linklike";
      b.textContent = label;
      b.addEventListener("click", onClick);
      li.append(b);
      list.append(li);
    };
    const kb = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " kB");

    // Folders already fetched (as promises, so a click on one still loading just waits for it). Cleared each
    // time the window opens, so what it shows is never older than that.
    let seen = new Map();
    const fetchFolder = (car, folder) => {
      const kind = storageOf();
      const key = kind + "/" + car + "/" + (folder || "");
      if (!seen.has(key)) {
        const url = browseUrls[kind].replace(/0$/, car) + (folder ? "?folder=" + encodeURIComponent(folder) : "");
        const asked = fetch(url).then(async (res) => {
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || (kind === "local" ? "Couldn't open the invoice folder." : "Couldn't open OneDrive."));
          return data;
        });
        seen.set(key, asked);
        asked.catch(() => seen.delete(key));      // a failure is tried again next time
      }
      return seen.get(key);
    };

    async function show(folder) {
      const ticket = ++loading;
      status.textContent = "Loading…";
      list.textContent = "";
      const car = form.elements.car_id.value;
      if (!car) { status.textContent = "Pick the car first."; pathBox.textContent = ""; return; }
      let data;
      try {
        data = await fetchFolder(car, folder);
      } catch (e) {
        if (ticket === loading) status.textContent = e.message;
        return;
      }
      if (ticket !== loading) return;      // a newer click has replaced this listing
      pathBox.textContent = data.path;
      status.textContent = data.folders.length || data.files.length ? "" : "No folders or PDFs in here.";
      if (!data.is_root) entry("⬑ Up", () => show(data.parent_id || "root"));
      data.folders.forEach((f) => entry("📁 " + f.name, () => show(f.id)));
      data.files.forEach((f) => entry("📄 " + f.name + " (" + kb(f.size) + ")", () => choose(f)));
      // Going up is the likeliest next move: have the folder above ready while this one is read.
      if (!data.is_root) fetchFolder(car, data.parent_id || "root").catch(() => {});
    }

    function choose(file) {
      picker.close();
      if (pdfInput) pdfInput.value = "";      // the file picked here replaces any chosen on this computer
      const kind = storageOf();
      if (kind === "local") {
        // No link to fill in: the expense keeps the file's path, and the app serves it.
        const stored = document.getElementById("stored-invoice");
        const link = document.getElementById("stored-invoice-link");
        form.elements.invoice_path.value = file.id;
        form.elements.remove_invoice.checked = false;
        link.textContent = file.name;
        link.href = stored.dataset.fileUrl + "?path=" + encodeURIComponent(file.id);
        stored.hidden = false;
      } else {
        form.elements.invoice_url.value = file.web_url;
      }
      pickHint.textContent = "Linked: " + file.name;
      if (prefillHint && form.dataset.prefillUrl) {
        const body = new FormData();
        body.append(kind + "_id", file.id);
        body.append("name", file.name);
        prefillFrom(body);
      }
    }

    pickButton.addEventListener("click", () => {
      seen = new Map();
      picker.showModal();
      show(null);
    });
    document.getElementById("onedrive-picker-close").addEventListener("click", () => picker.close());
    // The car decides where the window starts: a different car means a different folder.
    form.elements.car_id.addEventListener("change", () => { seen = new Map(); showStorage(); if (picker.open) show(null); });
  }
  // New expense with nothing typed yet: fill automatically.
  if (!odo.value) fill(false);
});

// A box that takes a file by dropping it as well as by the button: `<div class="drop" data-drop-ext=".csv">` with its
// file input and a hidden `<small class="drop-hint">`. The hint is shown here, so without JavaScript it never promises
// a drop that can't work. The extension (default .pdf) is what is accepted; anything else dropped is ignored.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".drop[data-drop-ext]").forEach((box) => {
    const input = box.querySelector("input[type=file]");
    const ext = (box.dataset.dropExt || ".pdf").toLowerCase();
    const hint = box.querySelector(".drop-hint");
    if (!input) return;
    if (hint) hint.hidden = false;
    ["dragenter", "dragover"].forEach((name) => box.addEventListener(name, (ev) => {
      ev.preventDefault();
      box.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((name) => box.addEventListener(name, () => box.classList.remove("over")));
    box.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const file = [...((ev.dataTransfer && ev.dataTransfer.files) || [])]
        .find((f) => f.name.toLowerCase().endsWith(ext) || (ext === ".pdf" && f.type === "application/pdf"));
      if (!file) return;
      const chosen = new DataTransfer();
      chosen.items.add(file);
      input.files = chosen.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
});

// Import pages: a category made without leaving the page ("New category…" in a row's list), and the lists read
// again when the page comes back into view, for categories and service items made in another tab. Only options
// are added: nothing chosen or typed on the page changes.
document.addEventListener("DOMContentLoaded", () => {
  // A long form that came back with errors: start at the list of them.
  const summary = document.getElementById("error-summary");
  if (summary) summary.focus();

  const form = document.getElementById("import-form");
  const lists = document.getElementById("import-lists");
  const dialog = document.getElementById("category-dialog");
  if (!form || !lists || !dialog) return;
  const NEW = "__new__";
  const carKind = form.dataset.carKind || "";
  const suits = { electric: ["BEV", "PHEV"], combustion: ["ICE", "PHEV", "HEV"] };
  const selects = () => [...form.querySelectorAll("select[name^=category_id_]")];
  const addCategory = (select, c) => {
    const wanted = c.name.toLowerCase();
    if ([...select.options].some((o) => o.value === String(c.id) || o.textContent.trim().toLowerCase() === wanted)) return;
    if (carKind && suits[c.applies] && !suits[c.applies].includes(carKind)) return;
    const option = new Option(c.name, String(c.id));
    Object.assign(option.dataset, { period: c.period ? "1" : "0", service: c.service ? "1" : "0",
      gstfree: c.gstfree ? "1" : "0", purchase: c.purchase ? "1" : "0", applies: c.applies });
    select.add(option, select.querySelector(`option[value="${NEW}"]`));
  };
  // Rows are also added while a folder is being read, so each list gets its "New category…" as it appears.
  const decorate = () => selects().forEach((select) => {
    if (!select.querySelector(`option[value="${NEW}"]`)) select.add(new Option("New category…", NEW));
    if (select.dataset.last === undefined) select.dataset.last = select.value;
  });
  decorate();
  new MutationObserver(decorate).observe(form, { childList: true, subtree: true });

  const dialogForm = document.getElementById("category-dialog-form");
  const status = document.getElementById("category-dialog-status");
  let opener = null;
  // Captured first, so nothing else takes "New category…" for a category.
  form.addEventListener("change", (e) => {
    const select = e.target;
    if (!/^category_id_\d+$/.test(select.name || "")) return;
    if (select.value !== NEW) { select.dataset.last = select.value; return; }
    e.stopPropagation();
    select.value = select.dataset.last || "";
    opener = select;
    dialogForm.reset();
    status.textContent = "";
    dialog.showModal();
    dialogForm.elements.name.focus();
  }, true);
  document.getElementById("category-dialog-cancel").addEventListener("click", () => dialog.close());
  dialogForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    status.textContent = "Adding…";
    try {
      const res = await fetch(dialog.dataset.categoryUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: dialogForm.elements.name.value }),
      });
      const made = await res.json();
      if (!res.ok) { status.textContent = made.error || "Couldn't add it."; return; }
      selects().forEach((select) => addCategory(select, made));
      const match = [...opener.options].find((o) => o.value === String(made.id));
      if (match) {
        opener.value = match.value;
        opener.dispatchEvent(new Event("change", { bubbles: true }));
      }
      dialog.close();
    } catch (err) {
      status.textContent = "Couldn't add it. Try again.";
    }
  });

  const refresh = async () => {
    try {
      const res = await fetch(lists.dataset.listsUrl);
      if (!res.ok) return;
      const got = await res.json();
      selects().forEach((select) => got.categories.forEach((c) => addCategory(select, c)));
      form.querySelectorAll("[data-service-fields]").forEach((fields) =>
        got.service_items.forEach((item) => form.addServiceItem && form.addServiceItem(fields, item, false)));
    } catch (err) { /* offline: the lists stay as they are */ }
  };
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
});
