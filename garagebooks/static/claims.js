// The claim page: the sign-in to the provider, and a note while the connector works. No inline scripts.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-claim-wait]").forEach((form) =>
    form.addEventListener("submit", () => {
      const note = form.querySelector(".claim-wait") || document.querySelector(".claim-wait");
      if (note) note.hidden = false;
      // After the browser has taken the form's values, so a disabled button can't stop the submit.
      setTimeout(() => form.querySelectorAll("button[type=submit]").forEach((b) => (b.disabled = true)), 0);
    }));

  // A sync running in the background: look every few seconds, and reload the page when it has finished.
  const running = document.getElementById("sync-running");
  if (running) {
    const look = async () => {
      try {
        const state = await (await fetch(running.dataset.statusUrl)).json();
        if (!state.running) { window.location.assign(window.location.pathname); return; }
      } catch (e) { /* try again */ }
      setTimeout(look, 4000);
    };
    setTimeout(look, 4000);
  }

  const box = document.getElementById("claim-signin");
  if (!box) return;
  const message = box.querySelector("[data-signin-message]");
  const failure = box.querySelector("[data-signin-error]");
  const start = box.querySelector("[data-signin-start]");
  const codeForm = box.querySelector("[data-signin-code]");
  const show = (el, text) => { el.textContent = text || ""; el.hidden = !text; };
  const send = async (url, body) => {
    try {
      const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      return await resp.json();
    } catch (e) {
      return { ok: false, error: "The connector can't be reached." };
    }
  };
  let timer = null;
  let giveUp = 0;
  const poll = async () => {
    let state = null;
    try {
      state = await (await fetch(box.dataset.statusUrl)).json();
    } catch (e) { /* try again on the next tick */ }
    const signin = (state && state.signin) || {};
    if (signin.state === "signed_in") {
      clearInterval(timer);
      window.location.assign(box.dataset.retryUrl);
      return;
    }
    if (signin.state === "failed") {
      clearInterval(timer);
      show(failure, signin.error || "Signing in failed.");
      show(message, "");
      codeForm.hidden = true;
      start.disabled = false;
      return;
    }
    if (signin.state === "code_needed") {
      show(message, signin.message || "Enter the code the provider sent you.");
      codeForm.hidden = false;
    } else if (signin.state === "starting" || signin.state === "checking") {
      show(message, signin.message || "Working on it…");
    }
    if (Date.now() > giveUp) {
      clearInterval(timer);
      show(failure, "That took too long. Try signing in again.");
      start.disabled = false;
    }
  };
  const watch = () => {
    clearInterval(timer);
    giveUp = Date.now() + 4 * 60 * 1000;
    timer = setInterval(poll, 2000);
  };
  start.addEventListener("click", async () => {
    show(failure, "");
    start.disabled = true;
    const out = await send(box.dataset.startUrl);
    if (!out.ok) { show(failure, out.error || "Signing in couldn't be started."); start.disabled = false; return; }
    show(message, "Signing in…");
    watch();
  });
  codeForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    show(failure, "");
    const out = await send(box.dataset.codeUrl, { code: codeForm.elements.code.value });
    if (!out.ok) { show(failure, out.error || "The code couldn't be sent."); return; }
    codeForm.hidden = true;
    show(message, "Checking the code…");
    watch();
  });
});
