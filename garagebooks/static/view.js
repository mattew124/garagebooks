// Loaded in <head>, so the rows/tiles choice and any column widths the person has set are in place before
// the page first draws (no flash of the other layout). No inline scripts: the CSP only allows files.
// app.js wires up the buttons and the dragging.
(function () {
  var root = document.documentElement;
  root.classList.add("js");   // the header's menu button and Settings dropdown only exist with scripting
  var saved = null;
  try { saved = localStorage.getItem("expenses-view"); } catch (e) { /* private mode */ }
  root.dataset.view = saved === "rows" || saved === "tiles" ? saved
    : (matchMedia("(max-width: 40rem)").matches ? "tiles" : "rows");

  // Column widths for the rows layout, in pixels by column: {date: 120, cat: 160, ...}, or nothing for
  // the built-in ones. They go into custom properties that style.css reads (set through the CSSOM,
  // which the CSP allows where it would refuse a style attribute).
  // "claim" is only in the table on the Claims page; the others are on every expense list.
  var COLUMNS = ["date", "cat", "vendor", "amount", "odo", "inv", "claim", "act"];
  var REQUIRED = ["date", "cat", "vendor", "amount", "odo", "inv", "act"];
  window.expenseColumns = {
    names: COLUMNS,
    apply: function (widths) {
      var total = 0;
      var good = function (name) { return !!widths && typeof widths[name] === "number" && widths[name] >= 20 && widths[name] <= 2000; };
      var ok = REQUIRED.every(good);
      var claims = !!document.querySelector("col.c-claim");      // the Claims page has one more column
      COLUMNS.forEach(function (name) {
        if (ok && good(name)) { root.style.setProperty("--col-" + name, widths[name] + "px"); if (name !== "claim" || claims) total += widths[name]; }
        else root.style.removeProperty("--col-" + name);
      });
      if (ok && claims && !good("claim")) total += 200;
      if (ok) root.style.setProperty("--cols-total", total + "px");
      else root.style.removeProperty("--cols-total");
      return ok;
    },
  };
  try { window.expenseColumns.apply(JSON.parse(localStorage.getItem("expenses-columns"))); } catch (e) { /* built-in widths */ }
})();
