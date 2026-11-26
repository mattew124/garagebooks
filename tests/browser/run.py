"""Browser tests against the running dev stack: `scripts/dev.sh test-browser`.

Drives real Chromium through the UI on a phone viewport and renders the Grafana
dashboard, reaching the containers directly on the dev network. Screenshots go
to .dev/screenshots. Everything it creates has a marker vendor and is deleted
again, so test expenses you entered by hand are left alone.
"""

import os
import re
import sys
import time

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("BASE_PATH", "/garagebooks-dev")
UI = f"http://garagebooks-dev:8080{BASE}/"
GRAFANA = f"http://dev-grafana:3000{BASE}/grafana/"
OUT = os.environ.get("OUT", "/out")
MARKER = "BROWSER-TEST"
INVOICE = "https://example.com/invoice-browser-test.pdf"
# Writes to a real OneDrive happen only in this folder (scripts/dev.sh onedrive-link points here).
TEST_FOLDER = "Expenses test"
REQUIRE_ONEDRIVE = os.environ.get("REQUIRE_ONEDRIVE") == "1"
# ONLY=header runs just the steps whose name contains it (for working on one thing; the full run is the check).
ONLY = os.environ.get("ONLY", "")
# Every run saves the names of the steps that failed in OUT/failed.txt. RERUN_FAILED=1 runs only those (and the
# clean-up), so a failure is checked again in a minute, not by the whole suite. A step that needs data an earlier
# step made can fail on its own: then run that step too (ONLY), or the whole suite.
FAILED_FILE = os.path.join(OUT, "failed.txt")
RERUN = None
if os.environ.get("RERUN_FAILED") == "1":
    try:
        with open(FAILED_FILE, encoding="utf-8") as f:
            RERUN = {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        RERUN = set()
passed, failed, skipped = 0, [], []


class Skip(Exception):
    """This check needs something the stack doesn't have. Reported, and a failure with --onedrive."""


def step(name, fn, *args):
    global passed
    if ONLY and ONLY not in name:
        return
    if RERUN is not None and name not in RERUN:
        return
    try:
        fn(*args)
        passed += 1
        print(f"ok   {name}")
    except Skip as why:
        if REQUIRE_ONEDRIVE:
            failed.append(name)
            print(f"FAIL {name}\n     required, but: {why}")
        else:
            skipped.append(name)
            print(f"SKIP {name}\n     {why}")
    except Exception as exc:  # keep going: report every failure in one run
        failed.append(name)
        print(f"FAIL {name}\n     " + "\n     ".join(str(exc).splitlines()[:12]))


def shot(page, name):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=True)


def add_expense(page, amount="123.45", category="Tyres"):
    page.goto(UI + "new")
    page.select_option("select[name=category_id]", label=category)
    page.fill("input[name=amount]", amount)
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=invoice_url]", INVOICE)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))


def test_duplicate_is_asked_about_before_saving(page):
    """Adding the same car/amount/date twice: the page asks first (inline, nothing sent), and
    Save anyway saves the second."""
    def fill():
        page.goto(UI + "new")
        page.select_option("select[name=category_id]", label="Tyres")
        page.fill("input[name=amount]", "246.81")
        page.fill("input[name=vendor]", MARKER)
        page.click("button[type=submit].button")
    fill()
    page.wait_for_url(re.compile(r"msg=saved"))                 # nothing like it yet: saves straight away
    fill()
    expect(page.locator("#duplicate-warning")).to_be_visible()
    expect(page.locator("#duplicate-list li")).to_have_count(1)
    assert "246.81" in page.inner_text("#duplicate-list")
    assert "/new" in page.url and "msg=" not in page.url        # still on the form, nothing saved
    shot(page, "duplicate-warning")
    page.click("#duplicate-save")
    page.wait_for_url(re.compile(r"msg=saved"))
    page.goto(UI)
    assert page.locator("tbody tr", has_text=MARKER).count() == 2


def test_duplicate_warning_can_be_dismissed(page):
    page.goto(UI + "new")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", "246.81")
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    expect(page.locator("#duplicate-warning")).to_be_visible()
    page.click("#duplicate-cancel")
    expect(page.locator("#duplicate-warning")).to_be_hidden()
    assert page.input_value("input[name=vendor]") == MARKER      # the form is untouched


def test_large_pdf_is_accepted(page):
    """Uploads over 500 KB are spooled to a temp file. The container's root is read-only, so
    this fails with 'No usable temporary directory' unless TMPDIR points at the tmpfs."""
    big = b"%PDF-1.4\n" + b"0" * (900 * 1024) + b"\n%%EOF\n"
    resp = page.request.post(UI + "api/prefill", headers={"Origin": "http://garagebooks-dev:8080"}, multipart={
        "invoice_file": {"name": "large.pdf", "mimeType": "application/pdf", "buffer": big}})
    assert resp.status == 200, f"HTTP {resp.status}: a large PDF upload crashed the app"
    assert set(resp.json()) >= {"date", "amount", "vendor", "category_id", "odometer_km"}


def wait_for_import(page, done):
    """Waits for the import page's status to match `done`. The test folder only grows (the app has no
    delete, and each upload check leaves a PDF there), and OneDrive takes seconds per PDF, so the time
    allowed follows the count the page reports ("Reading 3 of 54"), not a fixed limit."""
    status = page.locator("#import-status-text")
    done = re.compile(done)
    expect(status).to_have_text(re.compile(r"Reading \d+ of \d+|" + done.pattern), timeout=120000)   # the folder listed
    total = re.search(r"Reading \d+ of (\d+)", status.inner_text())
    expect(status).to_have_text(done, timeout=max(240000, int(total.group(1)) * 15000) if total else 240000)


def test_import_page_shows_progress(page):
    """The import page opens at once and reports its progress, instead of a blank wait."""
    page.goto(UI + "storage")
    link = page.locator("a[href*='onedrive/import/']").first
    if "Connected as" not in page.inner_text("main") or not link.count():
        raise Skip("dev stack isn't connected to OneDrive with a car folder")
    car = re.search(r"onedrive/import/(\d+)", link.get_attribute("href")).group(1)
    started = time.time()
    page.goto(UI + f"onedrive/import/{car}", wait_until="domcontentloaded")
    assert time.time() - started < 5, "the import page should open at once, not wait for OneDrive"
    expect(page.locator("#import-status-text")).to_be_visible()
    # It ends with a summary (and the form ready) or a plain message saying why there is nothing to do.
    wait_for_import(page, r"^(Read \d+ PDF|Every PDF|No PDFs)")
    if page.locator("#import-form").is_visible():
        assert page.locator("#import-form button[type=submit]").is_enabled()
        shot(page, "import-progress")
        if in_test_folder(page, UI + f"onedrive/import/{car}"):
            page.goto(UI + f"onedrive/import/{car}", wait_until="domcontentloaded")
            wait_for_import(page, r"^Read \d+ PDF")
            assert MARKER in page.content(), "the import preview should list the PDF the upload check left there"
            check_skip_prompt(page)


def check_skip_prompt(page):
    """Pressing Import with files left unticked asks whether to skip them; OK ticks their Skip boxes.
    The POST is aborted, so nothing is saved or skipped in the dev database."""
    boxes = page.locator("#import-form input[name^=include_]")
    for i in range(boxes.count()):
        boxes.nth(i).uncheck()
    page.locator("#import-form input[name^=skip_]").first.check()
    assert page.inner_text("#import-form button[type=submit]") == "Skip 1", "the button should say it only skips"
    page.route("**/onedrive/import/*", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
    asked = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    page.click("#import-form button[type=submit]")
    page.unroute("**/onedrive/import/*")
    assert asked and "ticked to import" in asked[0], "Import with nothing ticked should offer to skip the files"
    skips = page.locator("#import-form input[name^=skip_]")
    assert all(skips.nth(i).is_checked() for i in range(skips.count())), "accepting should tick every Skip box"


def in_test_folder(page, back_to=None):
    """Whether every car points inside the test folder, the only place a test may write."""
    page.goto(UI + "storage")
    rows = [t.strip() for t in page.locator("ul.folders small").all_inner_texts()]
    if back_to:
        page.goto(back_to, wait_until="domcontentloaded")
    return bool(rows) and all(r.startswith("OneDrive: /" + TEST_FOLDER) for r in rows)


def test_real_upload(page):
    """Saves an expense with a PDF, through the real OneDrive, into the test folder only.
    The expense is deleted again; the file stays (the app has no delete) for the import check."""
    page.goto(UI + "storage")
    if not page.locator("button:has-text('Disconnect')").count():
        raise Skip("dev stack isn't connected to OneDrive (scripts/dev.sh onedrive-link)")
    if not in_test_folder(page):
        raise Skip(f"cars aren't pointed at /{TEST_FOLDER}, so nothing was written")
    page.goto(UI + "new")
    page.set_input_files("input[name=invoice_file]", {
        "name": "browser-test.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF\n"})
    page.wait_for_load_state("networkidle")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", "371.93")
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=invoice_url]", "")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"), timeout=60000)
    page.goto(UI)
    link = page.locator("tbody tr", has_text=MARKER).locator("a[href^='https://']").first.get_attribute("href")
    assert "example.com" not in link, "the invoice link should be the uploaded file's OneDrive address"
    remove_marked(page)


def test_onedrive_picker(page):
    expect.set_options(timeout=20000)       # real Microsoft takes seconds per folder
    try:
        picker_checks(page)
    finally:
        expect.set_options(timeout=5000)


def picker_checks(page):
    """The Add form's file picker (read-only): opens in the car's folder, goes up and back down,
    and choosing a PDF links it. Needs a connected drive whose car folder holds a PDF."""
    page.goto(UI + "storage")
    if not page.locator("button:has-text('Disconnect')").count():
        raise Skip("dev stack isn't connected to OneDrive (scripts/dev.sh onedrive-link)")
    # The form's picker opens whichever storage the car uses: only a car on OneDrive tests OneDrive's.
    if not any(t.strip().startswith("OneDrive:") for t in page.locator("ul.folders small").all_inner_texts()):
        raise Skip("no car uses a OneDrive folder (scripts/dev.sh onedrive-link)")
    page.goto(UI + "new")
    if not page.locator("select[name=car_id]").input_value():
        page.select_option("select[name=car_id]", index=1)
    expect(page.locator("#onedrive-pick")).to_be_visible()
    shot(page, "form-onedrive-button")
    page.click("#onedrive-pick")
    dialog = page.locator("#onedrive-picker")
    expect(dialog).to_be_visible()
    start = dialog.locator("#onedrive-picker-path")
    expect(start).not_to_have_text("")
    if "Choose a OneDrive folder" in dialog.locator("#onedrive-picker-status").inner_text():
        raise Skip("this car has no OneDrive folder")
    here = start.inner_text()
    expect(dialog.get_by_text("⬑ Up")).to_be_visible()    # in the car's folder, so there is a way up
    shot(page, "form-onedrive-picker")                     # before going up: the top of the drive has no Up
    dialog.get_by_text("⬑ Up").click()
    expect(start).not_to_have_text(here)
    expect(dialog.get_by_text("⬑ Up")).to_have_count(0)   # nothing above the top of the drive
    expect(dialog.locator("#onedrive-picker-list li button", has_text="📁").first).to_be_visible()
    dialog.get_by_text("Cancel").click()
    expect(dialog).not_to_be_visible()
    page.click("#onedrive-pick")
    expect(start).to_have_text(here)         # reopening starts at the car's folder again
    items = dialog.locator("#onedrive-picker-list li button")
    pdf = dialog.locator("#onedrive-picker-list li button", has_text="📄")
    if not pdf.count():
        # Uploads go into category subfolders, so look in each folder in turn, as a person would.
        names = [t[2:] for t in items.all_inner_texts() if t.startswith("📁")]
        for name in names:
            dialog.get_by_role("button", name="📁 " + name, exact=True).click()
            expect(dialog.locator("#onedrive-picker-path")).not_to_have_text(here)
            expect(dialog.locator("#onedrive-picker-status")).not_to_have_text("Loading…")
            if pdf.count():
                break
            dialog.get_by_text("⬑ Up").click()
            expect(dialog.locator("#onedrive-picker-path")).to_have_text(here)
    if not pdf.count():
        raise Skip("no PDF in the car's folder or the folders directly in it")
    pdf.first.click()
    expect(page.locator("input[name=invoice_url]")).to_have_value(re.compile(r"^https://"))


# ---- the invoice folder on the server ---------------------------------------------

LOCAL_FOLDER = "Browser test"      # the only folder in the dev stack's invoice volume these checks write to
ORIGIN = {"Origin": "http://garagebooks-dev:8080"}


def readable_pdf(text):
    """A one-page PDF a viewer will actually draw, with `text` on it."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def first_car(page):
    page.goto(UI + "storage")
    link = page.locator("ul.folders a[href*='local/browse/']").first
    if not link.count():
        raise Skip("the invoice folder isn't on in this stack (an image from before it existed?)")
    expect(page.locator("#local")).to_contain_text("Invoices are kept in")      # with the folder's path
    return re.search(r"local/browse/(\d+)", link.get_attribute("href")).group(1)


def car_folder(page, car):
    """Where the car's invoices go now, to put back afterwards: (storage, folder id) or None."""
    for kind in ("onedrive", "local"):
        resp = page.request.get(UI + f"api/{kind}/browse/{car}")
        if resp.status == 200:
            return kind, resp.json()["id"]
    return None


def restore_car_folder(page, car, before):
    if before is None:
        resp = page.request.post(UI + f"local/folders/{car}/clear", headers=ORIGIN)
    else:
        resp = page.request.post(UI + f"{before[0]}/folders/{car}", headers=ORIGIN, form={"folder_id": before[1]})
    assert resp.ok, f"couldn't put the car's folder back: HTTP {resp.status}"


def test_local_folder(page, pdf_page):
    """The whole of the invoice folder on the server, in a real browser: choose the car's folder with
    the picker, save an expense with a PDF, open that PDF from the list, pick it again from the Add
    form, and see it offered by the import. The car's own folder (OneDrive or not) is put back."""
    car = first_car(page)
    before = car_folder(page, car)
    try:
        local_folder_checks(page, pdf_page, car)
    finally:
        restore_car_folder(page, car, before)


def local_folder_checks(page, pdf_page, car):
    # The folder picker: from the top of the mount, make the test folder if this is the first run, use it.
    page.goto(UI + f"local/browse/{car}")
    expect(page.locator("h1")).to_contain_text("Folder for")
    expect(page.locator("p.here code")).to_have_text("/")
    expect(page.get_by_text("⬑ Up")).to_have_count(0)                # nothing above the mounted folder
    if page.get_by_role("link", name="📁 " + LOCAL_FOLDER, exact=True).count():
        page.get_by_role("link", name="📁 " + LOCAL_FOLDER, exact=True).click()
    else:
        page.fill("form.new-folder input[name=name]", LOCAL_FOLDER)
        page.click("form.new-folder button")
    expect(page.locator("p.here code")).to_have_text("/" + LOCAL_FOLDER)
    expect(page.get_by_text("⬑ Up")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "local-folder-browser")
    page.click("button:has-text('Use ')")
    page.wait_for_url(re.compile(r"/storage\?msg=folders"))
    expect(page.locator("ul.folders small").first).to_have_text(re.compile(rf"^Folder on this server: .*/{LOCAL_FOLDER}$"))
    expect(page.get_by_text("Import existing invoices").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "storage-page")

    # Save an expense with a PDF: it lands in the folder, and the list links to the app's own copy.
    page.goto(UI + "new")
    expect(page.locator("#onedrive-pick")).to_contain_text("Choose from the invoice folder")
    page.set_input_files("input[name=invoice_file]", {
        "name": "browser-test.pdf", "mimeType": "application/pdf", "buffer": readable_pdf(MARKER + " invoice")})
    page.wait_for_load_state("networkidle")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", "418.27")
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=invoice_url]", "")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    link = page.locator("tbody tr", has_text=MARKER).locator("a:has-text('PDF')").first
    href = link.get_attribute("href")
    assert re.search(r"/invoice/\d+$", href), f"the list should link to the stored invoice, not {href}"

    # The PDF itself: sent inline as a PDF, and the browser's own viewer draws it under the app's CSP.
    resp = page.request.get(UI.rstrip("/") + href[len(BASE):])
    assert resp.status == 200 and resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline") and resp.body().startswith(b"%PDF-")
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    saved = page.request.get(UI.rstrip("/") + href[len(BASE):] + "?download=1")
    assert saved.headers["content-disposition"].startswith("attachment")
    blocked, refused = [], []
    pdf_page.on("console", lambda m: blocked.append(m.text) if m.type == "error" and "404" not in m.text else None)
    # The browser also asks the site's root for a favicon, which is outside the app's path: not ours to answer.
    pdf_page.on("response", lambda r: refused.append(f"{r.status} {r.url}")
                if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)
    shown = pdf_page.goto(UI.rstrip("/") + href[len(BASE):])
    assert shown.status == 200 and shown.headers["content-type"] == "application/pdf"
    pdf_page.wait_for_timeout(2000)             # let the viewer draw before the picture is taken
    # Chromium shows a PDF in its own viewer, a built-in extension page framed inside the tab.
    assert any(f.url.startswith("chrome-extension://") for f in pdf_page.frames), \
        f"the PDF viewer didn't open: {[f.url for f in pdf_page.frames]}"
    pdf_page.screenshot(path=f"{OUT}/local-invoice-inline.png")
    assert not blocked and not refused, f"the browser reported errors showing the PDF: {blocked + refused}"

    # The edit page names the stored file and offers to remove it.
    page.locator("tbody tr", has_text=MARKER).locator("a:has-text('Edit')").first.click()
    expect(page.locator("#stored-invoice-link")).to_contain_text(MARKER)
    expect(page.locator("input[name=remove_invoice]")).not_to_be_checked()
    shot(page, "local-invoice-on-edit")

    # The Add form's file picker opens in the car's folder and links a PDF that is already there.
    page.goto(UI + "new")
    expect(page.locator("#stored-invoice")).to_be_hidden()          # nothing stored yet, so nothing to show
    page.click("#onedrive-pick")
    dialog = page.locator("#onedrive-picker")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#onedrive-picker-title")).to_contain_text("invoice folder")
    expect(dialog.locator("#onedrive-picker-path")).to_have_text("/" + LOCAL_FOLDER)
    dialog.get_by_role("button", name="📁 Maintenance", exact=True).click()     # where Tyres invoices go
    expect(dialog.locator("#onedrive-picker-path")).to_have_text(f"/{LOCAL_FOLDER}/Maintenance")
    shot(page, "form-local-picker")
    dialog.locator("#onedrive-picker-list li button", has_text="📄").first.click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("input[name=invoice_path]")).to_have_value(re.compile(rf"^/{LOCAL_FOLDER}/Maintenance/.+\.pdf$"))
    expect(page.locator("#stored-invoice")).to_be_visible()
    expect(page.locator("#stored-invoice-link")).to_have_attribute("href", re.compile(r"local/file\?path=%2F"))
    picked_path = page.locator("input[name=invoice_path]").input_value()

    # The import reads the folder; the PDF saved above already has its expense, so it isn't offered.
    page.goto(UI + f"local/import/{car}", wait_until="domcontentloaded")
    expect(page.locator("#import-status-text")).to_have_text(re.compile(r"^(Read \d+ PDF|Every PDF)"), timeout=120000)
    assert "418.27" not in page.content()
    shot(page, "local-import")
    import_row_on_a_phone(page, car, picked_path)


def import_row_on_a_phone(page, car, path):
    """An import row on a phone: Notes, the period and the GST each sit on their own full-width line. The row is read
    from the PDF already in the folder (the read doesn't care that it has an expense) and put into the page."""
    size = page.viewport_size
    page.set_viewport_size({"width": 390, "height": 900})
    try:
        page.goto(UI + f"local/import/{car}", wait_until="domcontentloaded")
        expect(page.locator("#import-status-text")).to_have_text(re.compile(r"^(Read \d+ PDF|Every PDF)"), timeout=120000)
        html = page.evaluate("""async ([url, path]) => {
            const body = new FormData();
            body.append("n", "0"); body.append("id", path); body.append("url", path);
            body.append("name", path.split("/").pop()); body.append("folder", "");
            return await (await fetch(url, {method: "POST", body})).text();
        }""", [UI + f"local/import/{car}/read", path])
        page.evaluate("""(html) => {
            const form = document.getElementById("import-form");
            form.hidden = false;
            form.querySelector("tbody").insertAdjacentHTML("beforeend", html);
        }""", html)
        row = page.locator("#import-form tbody tr").last
        row.locator("select[name^=category_id_]").select_option(label="Registration")     # shows the period (and GST)
        boxes = {}
        for name in ("notes_", "period_start_", "period_end_", "gst_amount_"):
            box = row.locator(f"[name^={name}]").first
            if box.count() and box.is_visible():
                boxes[name] = box.bounding_box()
        assert {"notes_", "period_start_", "period_end_"} <= set(boxes), boxes
        tile = row.bounding_box()
        for name, rect in boxes.items():
            assert rect["width"] > tile["width"] * 0.6, (name, rect, tile)           # not squeezed beside another field
        tops = [round(r["y"]) for r in boxes.values()]
        assert len(set(tops)) == len(tops), f"fields share a line on a phone: {boxes}"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        shot(page, "import-row-phone")
    finally:
        page.set_viewport_size(size)


def remove_expenses(page):
    page.goto(UI)
    while page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").count():
        page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first.click()
        if page.locator("#claim-record").count():        # a claimed expense can't be deleted until its record goes
            page.once("dialog", lambda d: d.accept())
            page.click("#claim-record button")
            page.wait_for_url(re.compile(r"msg=claim_forgotten"))
            continue
        page.once("dialog", lambda d: d.accept())
        page.click("button.danger")
        page.wait_for_url(re.compile(r"msg=deleted"))


def remove_marked(page):
    remove_expenses(page)
    page.goto(UI + "cars")                      # and a car a stopped run added by hand
    while page.locator(".car", has_text=MARKER).count():
        car = page.locator(".car", has_text=MARKER).first
        car.locator("summary", has_text="Edit").click()
        page.once("dialog", lambda d: d.accept())
        car.get_by_role("button", name="Delete car").click()
        page.wait_for_url(re.compile(r"msg=car_deleted"))
        page.goto(UI + "cars")


def test_history_page(page):
    """Add, edit and delete an expense, then see all three on the History page, newest first, with
    the edit shown as old -> new. (The log is append-only: these test entries stay in the dev stack.)"""
    add_expense(page)
    page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first.click()
    page.fill("input[name=amount]", "200")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=updated"))
    remove_marked(page)
    page.get_by_role("button", name="Menu").click()   # on a phone History is in the menu
    page.click("header nav a:has-text('History')")
    expect(page.locator("h1")).to_have_text("Change history")
    first = page.locator("li.change").first
    expect(first).to_contain_text("Deleted")
    expect(first).to_contain_text(MARKER)
    mine = page.locator(f"li.change:has-text('{MARKER}')")
    expect(mine.nth(1)).to_contain_text("$123.45 \u2192 $200.00")
    expect(mine.nth(2)).to_contain_text("Added")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll"
    shot(page, "history-phone")
    page.select_option("select[name=operation]", "delete")
    page.wait_for_url(re.compile(r"operation=delete"))
    expect(page.locator("li.change:has-text('Edited')")).to_have_count(0)


def test_history_desktop(page):
    """The History page at desktop width, light and dark, for looking at (entries come from the step before)."""
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.goto(UI + "history")
        expect(page.locator("li.change").first).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        shot(page, f"history-desktop-{scheme}")
    page.emulate_media(color_scheme="light")


def test_form(page):
    page.goto(UI + "new")
    shot(page, "form-phone")
    expect(page.locator("h1")).to_have_text("Add expense")
    expect(page.locator("select[name=car_id] option")).not_to_have_count(0)
    assert page.locator("select[name=category_id] option").count() >= 7      # the standard categories


ACAR_CSV = (
    '\ufeffMetadata\n"Date Format","Time Format"\n"d/M/yyyy","HH:mm"\n\n\nVehicles\n"Name","Make","Model","Year"\n'
    '"Example","Example","Hatch","2020"\n\n\nFill-Up Records\n'
    '"Vehicle","Date","Time","Odometer Reading","Distance Unit","Volume","Volume Unit","Price per Unit","Total Cost",'
    '"Partial Fill-Up?"\n"Example","1/3/2026","08:00","19,400","km","40.000","L","$2.000","$80.00","Yes"\n\n\nEvent Records\n'
    '"Vehicle","Date","Time","Odometer Reading","Distance Unit","Type","Sub Types","Total Cost","Payment","Place Name","Notes"\n'
    '"Example","19/2/2026","14:10","19,281","km","Service","Engine Oil, Inspection","$429.30","","Example Garage",""\n'
    '"Example","1/6/2026","23:59","20,500","km","Sold","","$2,500.00","","",""\n')


def test_wheel_ticks(page):
    """Which wheels: offered only under a ticked per-wheel item, or for Tyres. Nothing is saved."""
    page.goto(UI + "new")
    tyres = page.locator("form [data-wheels-field]")
    page.select_option("select[name=category_id]", label="Tyres")
    expect(tyres).to_be_visible()
    page.select_option("select[name=category_id]", label="Service")
    expect(tyres).to_be_hidden()
    item = page.locator("#service-fields .service-item", has_text="Brakes").first
    ticks = item.locator(".wheels")
    expect(ticks).to_be_hidden()
    item.get_by_label("Brakes", exact=True).check()
    expect(ticks).to_be_visible()
    ticks.get_by_title("Rear left").click()
    expect(ticks.get_by_label("Rear left")).to_be_checked()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "wheel-ticks")


def test_acar_import_layout(page):
    """A car with many service items, on a phone that chose Rows for the expense list and on a desktop:
    the ticks wrap, the form stays inside the screen, and on a phone each cost is a card whose fields
    are as wide as it is. Nothing is saved."""
    items = ", ".join(f"Example Part {n}" for n in range(1, 19))
    csv = ACAR_CSV.replace('"Engine Oil, Inspection"', f'"{items}"')

    def preview(tab):
        tab.goto(UI + "import/acar")
        tab.select_option("select[name=car_id]", value="new")
        tab.set_input_files("input[name=file]", {"name": "aCar-records.csv", "mimeType": "text/csv", "buffer": csv.encode()})
        tab.click("button[type=submit]")
        expect(tab.locator("tr#row-0 [data-service-fields]")).to_be_visible()

    def width(tab, selector):
        return tab.locator(selector).first.bounding_box()["width"]

    page.goto(UI)
    saved = page.evaluate("localStorage.getItem('expenses-view')")
    size = page.viewport_size
    try:
        page.evaluate("localStorage.setItem('expenses-view', 'rows')")
        page.set_viewport_size({"width": 360, "height": 780})
        preview(page)
        shot(page, "acar-import-phone")
        # The fill-up's litres, price and partial tick came across, and fit the card.
        expect(tab_fuel := page.locator("tr#row-1 [data-fuel-fields]")).to_be_visible()
        assert (page.input_value("input[name=litres_1]"), page.input_value("input[name=unit_price_1]")) == ("40.00", "2.000")
        expect(page.locator("input[name=partial_1]")).to_be_checked()
        assert tab_fuel.bounding_box()["width"] <= 360
        # Another cost shows them only while its category records litres; a figure keeps them in view.
        other = page.locator("tr#row-0 [data-fuel-fields]")
        expect(other).to_be_hidden()
        page.select_option("select[name=category_id_0]", label="Fuel")
        expect(other).to_be_visible()
        page.select_option("select[name=category_id_0]", label="Service")
        expect(other).to_be_hidden()
        page.select_option("select[name=category_id_1]", label="Tyres")
        expect(tab_fuel).to_be_visible()
        table = width(page, "table.import-table")
        assert table <= 360, f"on a phone the table is {table}px wide"
        for field in ("select[name=category_id_0]", "input[name=vendor_0]", "input[name=amount_0]", "tr#row-0 .file"):
            assert width(page, field) <= 360, field
        for field in ("select[name=category_id_0]", "input[name=vendor_0]", "tr#row-0 .file"):
            assert width(page, field) >= 260, f"{field} is squeezed to {width(page, field)}px"
        assert page.evaluate("localStorage.getItem('expenses-view')") == "rows"      # the list's choice is kept

        page.set_viewport_size({"width": 1280, "height": 900})
        preview(page)
        shot(page, "acar-import-desktop")
        table, wrap = width(page, "table.import-table"), width(page, ".import .table-wrap")
        assert table <= wrap + 1, f"on a desktop the table is {table}px in {wrap}px"
        assert width(page, "tr#row-0 .file") >= 120, "the What column is squeezed"
    finally:
        page.set_viewport_size(size)
        page.evaluate("(v) => v === null ? localStorage.removeItem('expenses-view') : localStorage.setItem('expenses-view', v)", saved)


def test_acar_import_preview(page):
    """Upload an aCar CSV: the preview shows the costs and the sale, and nothing is saved."""
    page.goto(UI + "import/acar")
    page.select_option("select[name=car_id]", index=2)      # after "Choose a car…" and "Create a new car from this file"
    # The file box works as the Add page's does: its hint appears, and a file dropped on it is the one chosen.
    expect(page.locator("#acar-drop-hint")).to_have_text("You can also drop the CSV file onto this box.")
    page.evaluate("""(csv) => {
        const box = document.getElementById("acar-drop");
        const data = new DataTransfer();
        data.items.add(new File([csv], "aCar-records.csv", {type: "text/csv"}));
        box.dispatchEvent(new DragEvent("drop", {dataTransfer: data, bubbles: true, cancelable: true}));
    }""", ACAR_CSV)
    assert page.evaluate("document.querySelector('input[name=file]').files[0].name") == "aCar-records.csv"
    shot(page, "acar-upload")
    page.click("button[type=submit]")
    expect(page.locator("table.import-table tbody tr")).to_have_count(3)
    assert "Sale of the car" in page.inner_text("table.import-table")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "acar-import")
    # The service items sit under the category, not beside it, and what doesn't suit the car stays hidden.
    cell = page.locator("tr#row-0 td[data-label=Category]")
    under, above = cell.locator("[data-service-fields]").bounding_box(), cell.locator("select[name^=category_id_]").bounding_box()
    assert under["y"] >= above["y"] + above["height"] and above["height"] < 60, (above, under)
    assert page.evaluate("""[...document.querySelectorAll('.service-items label[hidden]')]
        .every((label) => label.offsetParent === null)""")

    # A category made from a row's own list: saved, chosen on that row and offered on the others, nothing lost.
    # The two requests are answered here: the app can't delete a category, so this test must not make one
    # (tests/test_acar_import.py covers the real ones).
    def made(name, ident):
        return {"id": ident, "name": name, "applies": "all", "period": False, "service": False, "gstfree": False, "purchase": False}
    page.route("**/api/categories", lambda route: route.fulfill(status=201, json=made(MARKER + " category", 32001)))
    page.route("**/api/import-lists", lambda route: route.fulfill(json={
        "categories": [made(MARKER + " elsewhere", 32002)], "service_items": []}))
    first = page.locator("table.import-table tbody tr").first
    first.locator("input[name^=vendor_]").fill("Typed before adding")
    first.locator("select[name^=category_id_]").select_option(value="__new__")
    expect(page.locator("#category-dialog")).to_be_visible()
    page.fill("#category-dialog input[name=name]", MARKER + " category")
    page.click("#category-dialog button[type=submit]")
    expect(page.locator("#category-dialog")).to_be_hidden()
    expect(first.locator("select[name^=category_id_] option:checked")).to_have_text(MARKER + " category")
    assert first.locator("input[name^=vendor_]").input_value() == "Typed before adding"
    # One made elsewhere turns up when the page comes back into view.
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(first.locator("select[name^=category_id_] option", has_text=MARKER + " elsewhere")).to_have_count(1)

    # A wrong row: the page comes back at the list of errors, whose link goes to the row. Nothing is saved.
    first.locator("input[name^=include_]").check()
    first.locator("input[name^=amount_]").fill("abc")
    page.get_by_role("button", name=re.compile("^Import")).click()
    expect(page.locator("#error-summary")).to_be_focused()
    shot(page, "acar-errors")
    page.locator("#error-summary a").first.click()
    expect(page.locator("tr#row-0")).to_be_in_viewport()

    page.unroute("**/api/categories")
    page.unroute("**/api/import-lists")

    page.goto(UI + "import/acar")                     # and again, making the car from the file
    page.select_option("select[name=car_id]", value="new")
    page.set_input_files("input[name=file]", {"name": "aCar-records.csv", "mimeType": "text/csv", "buffer": ACAR_CSV.encode()})
    page.click("button[type=submit]")
    expect(page.locator("#new-car input[name=car_name]")).to_have_value("Example")
    expect(page.locator("#new-car input[name=car_make]")).to_have_value("Example")
    shot(page, "acar-import-new-car")
    page.goto(UI)
    assert page.locator("tbody tr", has_text="Example Garage").count() == 0
    page.goto(UI + "cars")
    assert page.locator(".car", has_text="Hatch").count() == 0      # previewing made no car


def test_odometer_autofills(page):
    page.goto(UI + "new")
    expect(page.locator("#odometer-hint")).to_have_text("Filled from TeslaMate.")
    assert float(page.input_value("input[name=odometer_km]")) > 0


def test_odometer_button_for_past_date(page):
    page.goto(UI + "new")
    expect(page.locator("#odometer-hint")).to_have_text("Filled from TeslaMate.")
    latest = float(page.input_value("input[name=odometer_km]"))
    page.fill("input[name=date]", "2025-01-15")
    page.fill("input[name=time]", "12:00")
    page.fill("input[name=odometer_km]", "")
    page.click("#fill-odometer")
    expect(page.locator("input[name=odometer_km]")).not_to_have_value("")
    assert float(page.input_value("input[name=odometer_km]")) < latest


def test_bad_link_is_explained(page):
    page.goto(UI + "new")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", "10")
    page.fill("input[name=vendor]", MARKER + " keep me")
    page.fill("input[name=invoice_url]", "http://example.com/x.pdf")
    page.click("button[type=submit].button")
    expect(page.locator(".error")).to_contain_text("https://")
    expect(page.locator("input[name=vendor]")).to_have_value(MARKER + " keep me")
    shot(page, "form-error-phone")


def test_add_list_edit_delete(page):
    add_expense(page)
    row = page.locator(f"tr:has-text('{MARKER}')")
    expect(row).to_contain_text("$123.45")
    link = row.locator("a:has-text('PDF')")
    expect(link).to_have_attribute("href", INVOICE)
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    shot(page, "list-phone")
    row.locator("a:has-text('Edit')").click()
    page.fill("input[name=amount]", "200")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=updated"))
    expect(page.locator(f"tr:has-text('{MARKER}')")).to_contain_text("$200.00")
    remove_marked(page)
    expect(page.locator(f"tr:has-text('{MARKER}')")).to_have_count(0)


def test_delete_can_be_cancelled(page):
    add_expense(page, "50")
    page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").click()
    page.once("dialog", lambda d: d.dismiss())
    page.click("button.danger")
    page.goto(UI)
    expect(page.locator(f"tr:has-text('{MARKER}')")).to_have_count(1)
    remove_marked(page)


def test_phone_layout_has_no_sideways_scroll(page):
    for path in ("new", "categories"):
        page.goto(UI + path)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
    page.goto(UI + "categories")
    shot(page, "categories-phone")


def test_categories_save_together_and_delete_is_a_small_button(page):
    """One Save for the whole page, and a service item's x stays on its row, on a phone too."""
    page.goto(UI + "categories")
    expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(1)
    name, original = "Browser test item", page.viewport_size
    page.fill("input[name=name][placeholder='e.g. Spark plugs']", name)
    page.get_by_role("button", name="Add item").click()
    try:
        row = page.locator("li", has=page.locator(f"input[value='{name}']"))
        delete = row.get_by_role("button", name=f"Delete {name}")
        for size in ({"width": 360, "height": 740}, {"width": 1200, "height": 800}):
            page.set_viewport_size(size)
            field, button = row.locator("input[name^=item_name_]").bounding_box(), delete.bounding_box()
            assert abs((field["y"] + field["height"] / 2) - (button["y"] + button["height"] / 2)) < 6, (size, field, button)
            assert button["width"] < 60, button
        row.locator("input[name^=item_name_]").fill(name + " 2")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator(f"input[value='{name} 2']")).to_have_count(1)
        page.set_viewport_size({"width": 360, "height": 740})
        page.locator(f"input[value='{name} 2']").scroll_into_view_if_needed()
        shot(page, "categories-service-items-phone")
    finally:
        page.set_viewport_size(original)
        page.goto(UI + "categories")
        for n in ("Browser test item", "Browser test item 2"):     # also sweeps what an aborted earlier run left
            btn = page.get_by_role("button", name=f"Delete {n}", exact=True)
            if btn.count():
                asked = []
                page.once("dialog", lambda d, asked=asked: (asked.append(d.message), d.accept()))
                btn.click()
                page.wait_for_url(re.compile(r"msg=service_deleted"))
                assert asked and asked[0].startswith(f"Delete {n} "), asked
                page.wait_for_load_state()


def header_rows(page):
    """The distinct rows (by vertical centre) of the header's visible links, button and title."""
    return page.evaluate("""() => {
        const rows = new Set();
        for (const el of document.querySelectorAll('header .brand, header nav a, header .add, header button')) {
            const r = el.getBoundingClientRect();
            if (r.width && r.height) rows.add(Math.round((r.top + r.height / 2) / 10));
        }
        return rows.size;
    }""")


# Every page the header must reach, as (link text, path the page ends at).
HEADER_PAGES = [("All", ""), ("Categories", "categories"), ("Cars", "cars"), ("Reminders", "reminders"),
                ("History", "history"), ("Settings", "settings"), ("Invoice storage", "storage")]


def go_via_header(page, name, path, narrow):
    page.goto(UI)
    if narrow:
        page.get_by_role("button", name="Menu").click()
    elif name in ("Settings", "Invoice storage"):
        page.locator("header .drop-toggle").click()
    page.locator("header").get_by_role("link", name=name, exact=True).first.click()
    page.wait_for_url(re.compile(re.escape(UI + path) + r"(\?.*)?$"))


def test_header_stays_tidy_on_a_phone(page):
    """A phone shows the title, All, Reminders, Add and the menu button on one row; the rest opens from the
    button, with Settings and Invoice storage in it, and every page can be reached."""
    page.goto(UI)
    assert header_rows(page) == 1, "the closed header should be one row"
    visible = [a.inner_text().split("\n")[0].strip() for a in page.locator("header nav a:visible").all()]
    assert visible == ["All", "Reminders"], visible
    button = page.get_by_role("button", name="Menu")
    assert button.get_attribute("aria-expanded") == "false"
    assert button.get_attribute("aria-controls") == "site-menu"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "header-phone")
    button.click()
    assert button.get_attribute("aria-expanded") == "true"
    for name in ("Categories", "Cars", "History", "Settings", "Invoice storage"):
        expect(page.locator("header").get_by_role("link", name=name, exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "header-phone-open")
    page.keyboard.press("Escape")
    assert button.get_attribute("aria-expanded") == "false"
    for name, path in HEADER_PAGES:
        go_via_header(page, name, path, narrow=True)
    page.goto(UI + "settings")
    shot(page, "settings-phone")


def test_notifications_settings(page):
    """The Notifications section: the address is never shown back, a bad one is refused, and the test button
    says how it went. The address is a closed port on the app's own container (nothing leaves the stack),
    and is removed again afterwards."""
    topic = "browser-test-secret-topic"
    address = f"http://127.0.0.1:9/{topic}"
    page.goto(UI + "settings")
    section = page.locator("form[action$='/settings/notifications']")
    try:
        page.locator("form[action$='/settings/notifications/test'] button").click()
        expect(page.locator("main")).to_contain_text("Save an address first")
        section = page.locator("form[action$='/settings/notifications']")
        section.locator("input[name=notify_url]").fill("ftp://example.invalid/topic")
        section.locator("button[type=submit]").click()
        expect(page.locator(".error", has_text="Enter a full address")).to_be_visible()
        section = page.locator("form[action$='/settings/notifications']")
        section.locator("input[name=notify_url]").fill(address)
        section.locator("select[name=notify_format]").select_option("json")
        section.locator("button[type=submit]").click()
        expect(page.locator(".notice")).to_contain_text("Notification settings saved")
        html = page.content()
        assert topic not in html, "the saved address was shown back"
        expect(page.locator("form[action$='/settings/notifications'] small", has_text="Sending to")).to_contain_text("127.0.0.1:9")
        assert page.locator("input[name=notify_url]").get_attribute("type") == "password"
        page.locator("form[action$='/settings/notifications/test'] button").click()
        expect(page.locator("form[action$='/settings/notifications/test'] .error")).to_contain_text("could not be reached")
        assert topic not in page.content()
        shot(page, "settings-notifications")
    finally:
        page.goto(UI + "settings")
        section = page.locator("form[action$='/settings/notifications']")
        if section.locator("input[name=notify_clear]").count():
            section.locator("input[name=notify_clear]").check()
            section.locator("button[type=submit]").click()
            expect(page.locator(".notice")).to_contain_text("Notification settings saved")
    page.goto(UI + "settings")
    expect(page.locator("input[name=notify_clear]")).to_have_count(0)


def test_header_on_a_desktop(page):
    """One row: title then links on the left, Add on the right, Settings a dropdown holding Invoice storage,
    and every page reachable."""
    page.goto(UI)
    assert header_rows(page) == 1
    expect(page.get_by_role("button", name="Menu")).to_be_hidden()
    expect(page.locator("header").get_by_role("link", name="Invoice storage")).to_be_hidden()
    toggle = page.locator("header .drop-toggle")
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    expect(page.locator("header").get_by_role("link", name="Invoice storage")).to_be_visible()
    shot(page, "header-desktop-settings")
    page.keyboard.press("Escape")
    assert toggle.get_attribute("aria-expanded") == "false"
    shot(page, "header-desktop")
    for name, path in HEADER_PAGES:
        go_via_header(page, name, path, narrow=False)
        assert header_rows(page) == 1, name


def test_footer_links_to_the_docs_and_names_the_build(page):
    """Every page ends with the docs link and the version: a released image (a candidate under test)
    names its version, a local build says it is a development build."""
    page.goto(UI)
    footer = page.locator("footer")
    docs = footer.get_by_role("link", name="Documentation")
    assert docs.get_attribute("href") == "https://mattew124.github.io/garagebooks/"
    assert docs.get_attribute("rel") == "noopener noreferrer"
    assert re.search(r"Development build|Version \d+\.\d+\.\d+", footer.inner_text()), footer.inner_text()
    shot(page, "footer")


def test_list_shows_key_fields_on_a_phone(page):
    add_expense(page)
    row = page.locator(f"tr:has-text('{MARKER}')")
    width = page.viewport_size["width"]
    for text in ("$123.45", "Edit", "PDF"):
        box = row.get_by_text(text, exact=False).first.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, f"{text} is off-screen: {box}"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "list-phone")
    remove_marked(page)


def test_amount_calculator(page):
    page.goto(UI + "new")
    amount = page.locator("input[name=amount]")
    amount.fill("100+20.50*2")
    expect(page.locator("#amount-calc")).to_have_text("= $141.00")
    page.press("input[name=amount]", "Enter")             # works it out; doesn't save yet
    expect(amount).to_have_value("141.00")
    assert "/new" in page.url
    amount.fill("")                                       # the keys work from the phone keypad too
    amount.type("90")
    page.click("#amount-keys [data-key='/']")
    amount.type("4")
    page.click("#amount-keys [data-key='=']")
    expect(amount).to_have_value("22.50")
    amount.fill("12+")                                    # half a sum is left alone, never guessed at
    page.locator("input[name=vendor]").click()
    expect(amount).to_have_value("12+")
    amount.fill("-3000+500")                              # a rebate sum comes out negative, and settles
    page.press("input[name=amount]", "Enter")
    expect(amount).to_have_value("-2500.00")
    amount.fill("1000/8+2")                              # and a sum is settled on save
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    expect(page.locator(f"tr:has-text('{MARKER}')")).to_contain_text("$127.00")
    shot(page, "calculator")
    remove_marked(page)


def fill_part(page, n, face, mode="paid", value=""):
    """Fill row n (from 0) of the payment calculator, adding rows as needed."""
    rows = page.locator("#pay-calc .pay-row")
    while rows.count() <= n:
        page.click("#pay-add")
    row = rows.nth(n)
    row.locator("[data-part=face]").fill(face)
    row.locator("[data-part=mode]").select_option(mode)
    if mode != "cash":
        row.locator("[data-part=value]").fill(value)


def test_payment_calculator(page):
    page.goto(UI + "new")
    calc = page.locator("#pay-calc")
    expect(calc.locator(".pay-row").first).to_be_hidden()          # closed until asked for
    calc.locator("summary").click()
    expect(page.locator("#pay-use")).to_be_disabled()               # nothing to use yet
    # A $100 gift card bought for $95 (5% off) plus $42.50 cash: covers $142.50, really cost $137.50.
    fill_part(page, 0, "100", "off", "5")
    fill_part(page, 1, "42.50", "cash")
    expect(page.locator("#pay-covered")).to_have_text("$142.50")
    expect(page.locator("#pay-cost")).to_have_text("$137.50")
    expect(page.locator("#pay-saved")).to_contain_text("$5.00")
    # Amount paid mode, and cents rounding half up: 33.33 at 12.5% off is 29.16375, so 29.16.
    fill_part(page, 0, "100", "paid", "95")
    expect(page.locator("#pay-cost")).to_have_text("$137.50")
    fill_part(page, 0, "33.33", "off", "12.5")
    fill_part(page, 1, "0.10", "cash")
    expect(page.locator("#pay-cost")).to_have_text("$29.26")
    expect(page.locator("#pay-covered")).to_have_text("$33.43")
    # No floating-point slips: 0.1 + 0.2 face is exactly 0.30.
    fill_part(page, 0, "0.1", "cash")
    fill_part(page, 1, "0.2", "cash")
    expect(page.locator("#pay-covered")).to_have_text("$0.30")
    # A half-filled or invalid row stops it, and says why; empty rows are ignored.
    fill_part(page, 1, "20", "off", "150")
    expect(page.locator("#pay-use")).to_be_disabled()
    expect(page.locator("#pay-calc [role=alert]")).to_contain_text("Check")
    page.click("#pay-calc .pay-row >> nth=1 >> [data-part=remove]")
    fill_part(page, 0, "100", "off", "5")
    expect(page.locator("#pay-calc [role=alert]")).to_have_text("")
    assert page.locator("#pay-calc .pay-row").count() == 1
    fill_part(page, 1, "42.50", "cash")
    fill_part(page, 2, "", "cash")                                  # an empty row counts for nothing
    expect(page.locator("#pay-cost")).to_have_text("$137.50")
    shot(page, "payment-calculator")
    # Use this amount puts the true cost in the field, and the form posts only the amount.
    sent = []
    page.on("request", lambda r: sent.append(r.post_data or "") if r.method == "POST" else None)
    page.click("#pay-use")
    expect(page.locator("input[name=amount]")).to_have_value("137.50")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    posted = "".join(sent)
    names = set(re.findall(r'name="([^"]+)"', posted))
    assert {"amount", "vendor", "category_id"} <= names, names
    refs = {"card_payment_ref", "bpay_biller_code", "bpay_ref"}      # the payment references are not the calculator's
    assert not any(re.search(r"face|part|pay|mode|discount", n) for n in names - refs), names
    assert "137.50" in posted and "42.50" not in posted, posted
    expect(page.locator(f"tr:has-text('{MARKER}')")).to_contain_text("$137.50")
    remove_marked(page)


def test_payment_calculator_on_a_desktop(page):
    page.goto(UI + "new")
    page.locator("#pay-calc summary").click()
    fill_part(page, 0, "100", "off", "5")
    fill_part(page, 1, "42.50", "cash")
    shot(page, "payment-calculator-desktop")


def test_list_toggle_sort_and_return(page):
    add_expense(page, "10.00", "Tyres")
    add_expense(page, "30.00", "Insurance")
    add_expense(page, "20.00", "Tyres")
    page.goto(UI)
    box = page.locator("html")
    expect(box).to_have_attribute("data-view", "tiles")           # a phone starts on tiles
    page.click("[data-view-choice=rows]")
    expect(box).to_have_attribute("data-view", "rows")
    assert page.locator("thead").is_visible()
    page.reload()
    expect(box).to_have_attribute("data-view", "rows")            # remembered
    page.click("[data-view-choice=tiles]")
    expect(page.locator("thead")).to_be_hidden()
    shot(page, "list-tiles")
    page.click("[data-view-choice=rows]")
    amounts = lambda: [t.strip() for t in page.locator(f"tr:has-text('{MARKER}') td.num:nth-child(4)").all_inner_texts()]
    # Changing a filter swaps the list in place: no reload, and nothing beside the box moves.
    page.evaluate("window.__same = 1")
    spots = lambda: page.evaluate("""[...document.querySelectorAll('.filter select, .view-toggle, .page-head .total')]
        .map(e => { const r = e.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width)]; })""")
    before = spots()
    page.select_option("select[name=sort]", "amount")
    page.wait_for_url(re.compile(r"sort=amount"))
    assert page.evaluate("window.__same") == 1, "the page reloaded"
    assert spots()[:-1] == before[:-1], (before, spots())
    columns = lambda: page.evaluate("[...document.querySelectorAll('thead th')].map(t => Math.round(t.getBoundingClientRect().width))")
    wide = columns()
    page.select_option("select[name=dir]", "asc")
    page.wait_for_url(re.compile(r"dir=asc"))
    assert amounts() == ["$10.00", "$20.00", "$30.00"], amounts()
    # on a phone the table scrolls sideways: sorting from a far column must not snap it back to the left
    # (the Odometer heading is no longer the last column, so bring it into view rather than scrolling to the end)
    page.evaluate("document.querySelector('th a.sort[href*=odometer]').scrollIntoView({inline: 'end', block: 'nearest'})")
    far = page.evaluate("document.querySelector('.table-wrap').scrollLeft")
    assert far > 0, "rows view should scroll sideways on a phone"
    page.click("th a.sort:has-text('Odometer')")
    page.wait_for_url(re.compile(r"sort=odometer"))
    assert page.evaluate("document.querySelector('.table-wrap').scrollLeft") == far, "scroll snapped back"
    page.click("th a.sort:has-text('Amount')")                    # a new column starts largest first
    page.wait_for_url(re.compile(r"sort=amount&dir=desc"))
    assert amounts() == ["$30.00", "$20.00", "$10.00"], amounts()
    shot(page, "list-sorted")
    # editing and saving, or cancelling, comes back to this category and order
    page.select_option("select[name=category]", label="Tyres")
    page.wait_for_url(re.compile(r"category=\d+"))
    assert columns() == wide, "columns changed width with the rows shown"
    assert page.evaluate("window.__same") == 1
    page.go_back()                                                # the browser's Back steps through the views
    page.wait_for_function("!location.search.includes('category')")
    page.go_forward()
    page.wait_for_url(re.compile(r"category=\d+"))
    back = page.url
    # Back then Forward asks for two lists: the one shown must be the last one asked for, whichever arrived first.
    expect(page.locator("select[name=category] option:checked")).to_have_text("Tyres")
    expect(page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first).to_have_attribute("href", re.compile(r"category=\d+"))
    page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first.click()
    page.click("text=Cancel")
    page.wait_for_url(back)
    page.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first.click()
    page.fill("input[name=amount]", "25")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"category=\d+.*sort=amount.*dir=desc.*msg=updated"))
    expect(page.locator("select[name=category] option:checked")).to_have_text("Tyres")
    page.evaluate("localStorage.clear()")                          # the choice is remembered: don't leave it for other tests
    remove_marked(page)


def test_pdf_can_be_dropped_on_the_form(page):
    """Dropping a PDF onto the invoice box chooses it, as the file button would, and reads it."""
    page.goto(UI + "new")
    if not page.locator("#invoice-drop").count():
        raise Skip("uploads are off in this stack (no GARAGEBOOKS_ENCRYPTION_KEY)")
    expect(page.locator("#invoice-drop-hint")).to_be_visible()
    page.evaluate("""() => {
        const drop = new DataTransfer();
        drop.items.add(new File(["%PDF-1.4\\n%%EOF\\n"], "dropped.pdf", {type: "application/pdf"}));
        document.getElementById("invoice-drop").dispatchEvent(
            new DragEvent("drop", {dataTransfer: drop, bubbles: true, cancelable: true}));
    }""")
    assert page.evaluate("[...document.querySelector('input[name=invoice_file]').files].map(f => f.name)") == ["dropped.pdf"]
    expect(page.locator("#prefill-hint")).not_to_have_text(re.compile("^Choose the PDF next"))


def test_desktop_look(page):
    """The list and the form at desktop width, light and dark, for looking at."""
    add_expense(page, "1180.00", "Tyres")
    add_expense(page, "-300.00", "Insurance")
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.goto(UI)
        expect(page.locator("html")).to_have_attribute("data-view", "rows")     # a desktop starts on rows
        card = page.locator(".card.list").bounding_box()
        assert card["width"] <= 1280 and page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        shot(page, f"list-desktop-{scheme}")
        page.click("[data-view-choice=tiles]")
        shot(page, f"list-desktop-tiles-{scheme}")
        page.evaluate("localStorage.clear()")
        page.goto(UI + "new")
        shot(page, f"form-desktop-{scheme}")
        page.goto(UI + "categories")
        shot(page, f"categories-desktop-{scheme}")
        page.goto(UI + "settings")
        shot(page, f"settings-desktop-{scheme}")
        page.goto(UI + "storage")
        shot(page, f"onedrive-desktop-{scheme}")
    page.emulate_media(color_scheme="light")
    remove_marked(page)


def test_rows_on_a_phone_show_whole_values(page):
    """Rows view on a phone: the date, the category and the amount are never cut short at the usual widths."""
    add_expense(page, "1234.56", "Registration")          # the longest standard category names
    add_expense(page, "64990.00", "Initial purchase")
    page.goto(UI)
    page.click("[data-view-choice=rows]")
    cut = page.evaluate("""marker => [...document.querySelectorAll('tbody tr')].filter(r => r.textContent.includes(marker))
        .flatMap(r => [...r.querySelectorAll('td.date, td.cat .chip, td.amount, td.odo, td.inv, td.actions')])
        .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className + ': ' + e.textContent.trim())""", MARKER)
    assert cut == [], f"cut short: {cut}"
    assert not re.search(r"\d\d:\d\d", page.locator(f"tr:has-text('{MARKER}') td.date").first.inner_text())
    shot(page, "list-rows-phone")
    page.evaluate("localStorage.clear()")
    remove_marked(page)


def test_column_widths_can_be_dragged(page):
    """Dragging a heading's edge changes that column only, is remembered, and a double-click undoes it."""
    add_expense(page)
    page.goto(UI)
    widths = lambda: page.evaluate("[...document.querySelectorAll('thead th')].map(t => Math.round(t.getBoundingClientRect().width))")
    usual = widths()
    handle = page.locator("thead th").nth(2).locator(".col-resize")       # Vendor
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 120, y, steps=4)
    page.mouse.up()
    wider = widths()
    assert abs(wider[2] - (usual[2] + 120)) <= 3, (usual, wider)
    assert all(abs(a - b) <= 2 for i, (a, b) in enumerate(zip(usual, wider, strict=True)) if i != 2), (usual, wider)
    assert "sort=" not in page.url, "dragging the edge must not sort the column"
    page.reload()
    assert widths() == wider, "the widths were not remembered"
    page.select_option("select[name=sort]", "amount")                      # and they survive the list being swapped
    page.wait_for_url(re.compile(r"sort=amount"))
    assert widths() == wider
    shot(page, "list-columns-resized")
    page.locator("thead th").nth(2).locator(".col-resize").focus()
    page.keyboard.press("ArrowLeft")
    assert widths()[2] == wider[2] - 16
    page.locator("thead th").nth(2).locator(".col-resize").dblclick()
    assert widths() == usual
    page.reload()
    assert widths() == usual
    remove_marked(page)


def test_onedrive_page(page):
    """The setup steps render on a phone, with the address to register. Only
    invalid details are submitted, so a real registration in the dev database
    is left alone."""
    page.goto(UI + "storage")
    expect(page.locator("h1")).to_have_text("Invoice storage")
    expect(page.locator("#onedrive h2")).to_have_text("OneDrive")
    expect(page.locator("#microsoft h2")).to_have_text("Microsoft account")   # the registration OneDrive and the mailbox share
    expect(page.locator("#mail h2")).to_have_text("Invoices by email")
    expect(page.locator("#cars h2")).to_have_text("Folder per car")       # one page for every storage
    expect(page.locator("#microsoft code.copy")).to_have_text(re.compile(re.escape(BASE) + r"/microsoft/callback$"))
    if not page.locator("#microsoft details[open]").count():
        page.click("#microsoft summary")
    page.fill("input[name=client_id]", "not-a-client-id")
    page.fill("input[name=client_secret]", "x")
    page.click("details button[type=submit]")
    expect(page.locator(".error").first).to_contain_text("Application (client) ID")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "onedrive-phone")
    page.goto(UI + "new")
    expect(page.locator("input[name=invoice_file]")).to_have_attribute("accept", "application/pdf,.pdf")
    shot(page, "form-onedrive-phone")


def test_settings_file_name_defaults_can_be_edited(page):
    """The defaults are real text, so typing adds to them instead of wiping a placeholder.
    Nothing is saved."""
    page.goto(UI + "settings")
    field = page.locator("input[name=invoice_name_format]")
    default = field.input_value()
    assert default == "%date %category - %vendor", default
    assert page.locator("input[name=invoice_date_format]").input_value() == "yymmdd"
    field.click()
    page.keyboard.press("End")
    page.keyboard.type(" %car")
    assert field.input_value() == default + " %car"
    shot(page, "settings-phone")


def test_settings_lists_the_scheduled_jobs(page):
    """Settings > Scheduled jobs: each built-in job with how often it runs and how its last run went. Read-only."""
    page.goto(UI + "settings")
    section = page.locator("#scheduled-jobs")
    assert section.locator("h2").text_content() == "Scheduled jobs", section.locator("h2").text_content()
    assert section.locator("#jobs-where").inner_text().strip(), "it says where the jobs run"
    names = section.locator("li[data-job]").evaluate_all("els => els.map(e => e.dataset.job)")
    assert names == ["ato_rates", "dashboard", "mail", "notifications", "onedrive_folders", "teslamate"], names
    states = section.locator("li[data-job]").evaluate_all("els => els.map(e => e.dataset.state)")
    assert set(states) <= {"never", "ok", "failed", "running"}, states
    assert "every hour" in section.locator('li[data-job="teslamate"]').inner_text()
    assert section.locator("button, input, form").count() == 0
    section.scroll_into_view_if_needed()
    shot(page, "settings-jobs")


def test_connect_reaches_microsoft(page):
    """Real Chromium enforces the CSP, so this is where a blocked redirect shows.
    The request to Microsoft is answered locally and never sent. Runs only when
    the dev database has an app registration saved."""
    page.goto(UI + "storage")
    button = page.locator("button:has-text('Connect OneDrive')")
    if not button.count():
        if page.locator("button:has-text('Disconnect')").count():
            print("     (not applicable: already connected)")
            return
        raise Skip("no app registration saved")
    reached = []

    def stub(route):
        reached.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<p>stub Microsoft sign-in</p>")
    page.route("https://login.microsoftonline.com/**", stub)
    button.click()
    page.wait_for_url(re.compile(r"^https://login\.microsoftonline\.com/consumers/oauth2/v2\.0/authorize\?"))
    assert reached and "code_challenge=" in reached[0]
    page.unroute("https://login.microsoftonline.com/**")


def test_folder_browser(page):
    """Read-only: when the dev stack is connected to a real OneDrive, open the
    folder browser for the first car. Creates nothing and changes no choice."""
    page.goto(UI + "storage")
    if not page.locator("button:has-text('Disconnect')").count():
        raise Skip("dev stack isn't connected to OneDrive")
    page.locator("ul.folders a[href*='onedrive/browse']").first.click()
    expect(page.locator("h1")).to_contain_text("Folder for")
    expect(page.locator("form.new-folder input[name=name]")).to_be_visible()
    expect(page.locator("button:has-text('Use ')")).to_be_visible()
    # Opening a folder and going back swaps the listing in place: a mark left on the window survives. Only the
    # test folder is opened from the top of the drive; elsewhere, going up.
    page.evaluate("window.samePage = true")
    here, start = page.locator("p.here code").inner_text(), page.url
    links = page.locator("ul.folder-list a")
    link = links.filter(has_text="Up" if here != "/" else "Expenses test").first
    expect(link).to_be_visible()
    link.click()
    expect(page.locator("p.here code")).not_to_have_text(here)
    assert page.evaluate("window.samePage"), "opening a folder loaded a new page"
    assert page.url != start
    page.go_back()
    expect(page.locator("p.here code")).to_have_text(here)
    assert page.evaluate("window.samePage"), "going back loaded a new page"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "folder-browser-phone")


def test_cars_page(page):
    """The app's own car list: TeslaMate's cars are there, and one can be added by hand, given an odometer
    reading from an expense, sold, and deleted again. The name is optional (the make and model name it)."""
    try:
        cars_checks(page)
    finally:
        remove_marked(page)


def cars_checks(page):
    page.goto(UI + "cars")
    expect(page.locator("#teslamate-status")).to_have_attribute("data-state", "ok")
    expect(page.locator(".car small", has_text="from TeslaMate").first).to_be_visible()
    page.fill("#add-car input[name=name]", MARKER)
    page.select_option("#add-car select[name=kind]", "ICE")
    page.fill("#add-car input[name=make]", "Example")
    page.click("#add-car button[type=submit]")
    page.wait_for_url(re.compile(r"msg=car_added"))
    car = page.locator(".car", has_text=MARKER)
    expect(car).to_contain_text("added by hand")
    expect(car.locator(".car-odometer")).to_contain_text("not recorded")
    page.goto(UI + "new")                                         # an odometer reading now comes from an expense
    page.select_option("select[name=car_id]", label=MARKER)
    page.wait_for_load_state("networkidle")                       # the odometer fills itself in on choosing a car
    expect(page.locator("select[name=category_id] option", has_text="Charging")).to_be_disabled()   # a petrol car doesn't plug in
    expect(page.locator("select[name=category_id] option", has_text="Fuel")).to_be_enabled()
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", "50")
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=odometer_km]", "12,345")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    page.goto(UI + "cars")
    car = page.locator(".car", has_text=MARKER)
    expect(car.locator(".car-odometer")).to_contain_text("12,345 km")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    expect(page.locator("summary", has_text="Odometer readings")).to_have_count(0)   # the readings section is gone
    shot(page, "cars-phone")
    car.locator("summary", has_text="Sold this car?").click()      # sell it: marked sold, then un-sold
    car.locator(".car-sale input[name=date]").fill("2020-01-02")
    car.locator(".car-sale input[name=price]").fill("-1")
    car.get_by_role("button", name="Save sale").click()
    expect(page.locator(".car", has_text=MARKER).locator(".error", has_text="negative")).to_be_visible()
    car = page.locator(".car", has_text=MARKER)
    car.locator(".car-sale input[name=price]").fill("$7,500")
    car.locator(".car-sale input[name=km]").fill("")               # a car without TeslaMate needs its final odometer
    car.get_by_role("button", name="Save sale").click()
    expect(page.locator(".car", has_text=MARKER).locator(".error").first).to_be_visible()
    car = page.locator(".car", has_text=MARKER)
    car.locator(".car-sale input[name=km]").fill("13,000")
    car.get_by_role("button", name="Save sale").click()
    page.wait_for_url(re.compile(r"msg=car_sold"))
    car = page.locator(".car", has_text=MARKER)
    expect(car.locator(".car-sold")).to_contain_text("Sold 02 Jan 2020 for $7,500.00")
    expect(car.locator(".car-sold")).to_contain_text("13,000 km")
    shot(page, "cars-sold-phone")
    page.goto(UI + "new")
    assert MARKER + " (sold)" in page.locator("select[name=car_id]").inner_text()
    page.goto(UI + "cars")
    car = page.locator(".car", has_text=MARKER)
    car.locator("summary", has_text="Sale").click()
    car.get_by_role("button", name="Clear sale").click()
    page.wait_for_url(re.compile(r"msg=car_unsold"))
    expect(page.locator(".car", has_text=MARKER).locator(".car-sold")).to_have_count(0)
    remove_expenses(page)                                          # a car with expenses can't be deleted
    page.goto(UI + "cars")
    car = page.locator(".car", has_text=MARKER)
    car.locator("summary", has_text="Edit").click()
    page.once("dialog", lambda d: d.accept())
    car.get_by_role("button", name="Delete car").click()
    page.wait_for_url(re.compile(r"msg=car_deleted"))
    expect(page.locator(".car", has_text=MARKER)).to_have_count(0)


def test_car_without_a_name(page):
    """A car added with only a make and a model is listed as "Make Model"; a purchase price and date typed
    into its Edit form show on the card as "Bought ... for ..."."""
    try:
        page.goto(UI + "cars")
        page.fill("#add-car input[name=make]", MARKER)
        page.fill("#add-car input[name=model]", "Model Q")
        assert page.input_value("#add-car input[name=name]") == ""
        page.click("#add-car button[type=submit]")
        page.wait_for_url(re.compile(r"msg=car_added"))
        car = page.locator(".car", has_text=MARKER)
        expect(car.locator("h2")).to_contain_text(f"{MARKER} Model Q")
        expect(car.locator(".car-bought")).to_have_count(0)
        car.locator("summary", has_text="Edit").click()
        car.locator("input[name=purchase_price]").fill("$41,234.56")
        car.locator("input[name=purchase_date]").fill("2024-05-06")
        car.locator("input[name=purchase_km]").fill("10")
        car.get_by_role("button", name="Save car").click()
        page.wait_for_url(re.compile(r"msg=car_saved"))
        car = page.locator(".car", has_text=MARKER)
        expect(car.locator(".car-bought")).to_contain_text("Bought 06 May 2024 for $41,234.56")
        expect(car.locator("h2")).to_contain_text(f"{MARKER} Model Q")
        shot(page, "cars-bought-phone")
        # The purchase is an expense with no vendor: give it the marker so the clean-up finds it.
        page.goto(UI)
        row = page.locator("tbody tr", has_text="41,234.56").first
        row.locator("a:has-text('Edit')").click()
        page.fill("input[name=vendor]", MARKER)
        page.click("button[type=submit].button")
        page.wait_for_url(re.compile(r"msg=updated"))
    finally:
        remove_marked(page)
    expect(page.locator(".car", has_text=MARKER)).to_have_count(0)


def lease_budgets(page, on):
    """Settings > Track lease budgets: the yearly budget boxes only exist while it is on."""
    page.goto(UI + "settings")
    box = page.locator("input[name=lease_budgets]")
    if box.is_checked() != on:
        box.set_checked(on)
        page.click("button[type=submit].button")
        page.wait_for_url(re.compile(r"msg=settings"))


def add_marked_car(page):
    page.goto(UI + "cars")
    page.fill("#add-car input[name=name]", MARKER)
    page.click("#add-car button[type=submit]")
    page.wait_for_url(re.compile(r"msg=car_added"))
    return page.locator(".car", has_text=MARKER)


def test_lease_page(page):
    was_on = None
    try:
        page.goto(UI + "settings")
        was_on = page.locator("input[name=lease_budgets]").is_checked()
        lease_budgets(page, True)             # the budget boxes this test fills only exist with the setting on
        lease_checks(page)
    finally:
        remove_marked(page)       # a failed run leaves no car behind to confuse the next checks
        if was_on is not None:
            lease_budgets(page, was_on)


def lease_checks(page):
    """A lease for a car added by hand: enter it from a statement, see the figures worked out, edit it,
    delete it. The car is deleted at the end, which takes anything left with it."""
    from datetime import date, timedelta
    today = date.today()
    car = add_marked_car(page)
    car.get_by_role("link", name="Lease").click()
    expect(page.locator("#no-lease")).to_be_visible()
    page.click("#add-lease-top")
    expect(page.locator("#f-start_date")).to_have_value("")        # not pre-filled any more
    expect(page.locator("#lease-budgets")).to_be_visible()          # the setting is on
    page.locator("#f-financier").fill("Example Finance")
    page.get_by_label("Lease start").fill((today - timedelta(days=30)).isoformat())
    page.locator("#f-end_date").fill((today + timedelta(days=1065)).isoformat())
    page.get_by_label("Term (months)").fill("36")
    page.get_by_label("Car's cost, value or financed amount").fill("60000")
    page.get_by_label("Residual percentage of cost").fill("46.875")
    page.get_by_label("Paid").select_option("fortnightly")
    page.get_by_label("Pre-tax part, per pay").fill("300")
    page.get_by_label("Post-tax part, per pay").fill("20")
    page.get_by_label("Provider's admin fee, per pay").fill("-5")
    page.get_by_role("button", name="Add lease").click()
    expect(page.locator("#e-admin_fee")).to_contain_text("negative")         # the error is beside the field...
    assert page.locator("#f-financier").input_value() == "Example Finance"    # ...and nothing typed is lost
    page.get_by_label("Provider's admin fee, per pay").fill("5")
    page.get_by_label("Pre-tax part, per pay").fill("300")
    page.get_by_label("Lease payments").fill("8450")
    page.get_by_label("Tyres").fill("400")
    page.get_by_role("button", name="Add lease").click()
    page.wait_for_url(re.compile(r"msg=lease_added"))
    body = page.inner_text("body")
    for shown in ("Example Finance", "$325.00", "$8,450.00", "$28,125.00", "46.875%", "not tax advice"):
        assert shown in body, f"{shown!r} is not on the lease page"
    assert re.search(r"10(64|65|66) days remaining", body), "days remaining is not shown"   # the app's date is Brisbane's
    expect(page.locator("[id^=residual-check]")).to_contain_text("meets it")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    shot(page, "lease-phone")
    page.get_by_role("link", name="Edit").click()
    page.locator("#f-financier").fill("Another Finance")
    page.get_by_role("button", name="Save lease").click()
    page.wait_for_url(re.compile(r"msg=lease_saved"))
    expect(page.locator(".lease")).to_contain_text("Another Finance")
    page.once("dialog", lambda d: d.dismiss())                                # asked first: No keeps it
    page.get_by_role("button", name="Delete").click()
    expect(page.locator(".lease")).to_contain_text("Another Finance")
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete").click()
    page.wait_for_url(re.compile(r"msg=lease_deleted"))
    expect(page.locator("#no-lease")).to_be_visible()
    page.goto(UI + "cars")
    car = page.locator(".car", has_text=MARKER)
    car.locator("summary", has_text="Edit").click()
    page.once("dialog", lambda d: d.accept())
    car.get_by_role("button", name="Delete car").click()
    page.wait_for_url(re.compile(r"msg=car_deleted"))


def lease_form_pdf(lines):
    """A one-page text PDF with one line of `lines` to a row: what `pdftotext -layout` reads back as the
    label and value lines of a provider's lease form (invented figures)."""
    esc = lambda t: t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = ("BT /F1 11 Tf 14 TL 40 760 Td " + " T* ".join(f"({esc(t)}) Tj" for t in lines) + " ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


LEASE_FORM_LINES = [
    "Mail To: Example Packaging Pty Ltd", "Associate Lease Form", "Section B - Vehicle Details",
    "Nominated Annual", "Kilometres 15,000", "FBT Base Cost $ 61,000", "Eligible EV -",
    "Section D - Lease Details", "Annual Lease Payment $13,000.00",
    "Lease Start Date 01-Jun-25", "Lease End Date 31-May-27", "Instalment Amount (ex GST) $ 500.00"]
LEASE_FORM_FILE = {"name": "browser-test-lease-form.pdf", "mimeType": "application/pdf",
                   "buffer": lease_form_pdf(LEASE_FORM_LINES)}


def car_id_of(car):
    return car.get_attribute("id").removeprefix("car-")


def open_add_lease(page, car):
    car.get_by_role("link", name="Lease").click()
    page.click("#add-lease-top")
    expect(page.locator("#lease-form")).to_be_visible()


def fill_basic_lease(page, start, term, financier=None):
    """The least a lease needs, typed in: the end date is worked out from the start and the term."""
    if financier:
        page.locator("#f-financier").fill(financier)
    page.get_by_label("Lease start").fill(start)
    page.get_by_label("Term (months)").fill(str(term))
    page.get_by_label("Paid").select_option("monthly")
    page.get_by_label("Pre-tax part, per pay").fill("100")


def test_lease_associate(page):
    """Associate lease: the financier and residual boxes go, the end follows the start and the term, a lease
    saves with no financier, and the page says what it is. Also the layout of the Add a lease page."""
    try:
        car = add_marked_car(page)
        open_add_lease(page, car)
        form = page.locator("#lease-form")
        assert form.locator("legend").count() == 0, "a legend is left inside the lease form"
        cards = form.locator("section.card")
        assert cards.count() >= 5
        for i in range(cards.count()):
            expect(cards.nth(i).locator("h2.card-title")).to_be_visible()
        expect(page.locator("#lease-budgets")).to_have_count(0)                  # budgets are off unless the setting is
        shot(page, "lease-add-phone")
        expect(page.locator("#f-financier")).to_be_visible()
        expect(page.locator("#f-residual_percent")).to_be_visible()
        page.locator("#f-associate_lease").check()
        expect(page.locator("#f-financier")).to_be_hidden()
        expect(page.locator("#f-residual_amount")).to_be_hidden()
        expect(page.locator("#f-residual_percent")).to_be_hidden()
        page.locator("#f-associate_lease").uncheck()
        expect(page.locator("#f-financier")).to_be_visible()
        page.locator("#f-associate_lease").check()
        fill_basic_lease(page, "2025-06-15", 24)
        expect(page.locator("#f-end_date")).to_have_value("2027-06-14")            # the day before the same date two years on
        page.get_by_role("button", name="Add lease").click()
        page.wait_for_url(re.compile(r"msg=lease_added"))
        expect(page.locator(".lease .associate")).to_have_text("Associate lease")
        assert page.locator(".lease dt", has_text="Financier").count() == 0
        assert page.locator(".lease dt", has_text="Residual").count() == 0
        # Edit: the box is ticked and the financier stays hidden.
        page.get_by_role("link", name="Edit").first.click()
        expect(page.locator("#f-associate_lease")).to_be_checked()
        expect(page.locator("#f-financier")).to_be_hidden()
    finally:
        remove_marked(page)


def local_test_folder(page):
    """The id of the invoice folder's "Browser test" folder (made if need be), or None when this stack has
    no invoice folder. Nothing is ever deleted from it."""
    page.goto(UI + "storage")
    link = page.locator("ul.folders a[href*='local/browse/']").first
    if not link.count():
        return None
    first = re.search(r"local/browse/(\d+)", link.get_attribute("href")).group(1)
    top = page.request.get(UI + f"api/local/browse/{first}?folder=root")
    if top.status != 200:
        return None
    for _attempt in range(2):
        found = [f for f in top.json()["folders"] if f["name"] == LOCAL_FOLDER]
        if found:
            return found[0]["id"]
        made = page.request.post(UI + f"local/browse/{first}/new-folder", headers=ORIGIN,
                                 form={"parent_id": top.json()["id"], "name": LOCAL_FOLDER})
        assert made.ok, f"couldn't make the {LOCAL_FOLDER} folder: HTTP {made.status}"
        top = page.request.get(UI + f"api/local/browse/{first}?folder=root")
    return None


def test_lease_form_import(page):
    """A lease form PDF chosen on the Add a lease page fills the boxes; with an invoice folder, the form and
    an agreement are kept and linked on the lease page."""
    try:
        car = add_marked_car(page)
        car_id = car_id_of(car)
        folder = local_test_folder(page)
        if folder is not None:
            chosen = page.request.post(UI + f"local/folders/{car_id}", headers=ORIGIN, form={"folder_id": folder})
            assert chosen.ok, f"couldn't choose the car's folder: HTTP {chosen.status}"
        page.goto(UI + "cars")
        open_add_lease(page, page.locator(".car", has_text=MARKER))
        # What the app reads from the PDF, before looking at the page.
        read = page.request.post(UI + "api/lease-prefill", headers=ORIGIN, multipart={"form_file": LEASE_FORM_FILE})
        assert read.ok, f"/api/lease-prefill: HTTP {read.status}"
        fields = read.json()["fields"]
        assert fields.get("start_date") == "2025-06-01" and fields.get("term_months") == "24", fields
        page.set_input_files("#f-form_file", LEASE_FORM_FILE)
        expect(page.locator("#lease-form [data-lease-document=form] [data-doc-hint]")).to_contain_text("Filled from the lease form")
        expect(page.locator("#f-start_date")).to_have_value("2025-06-01")
        expect(page.locator("#f-end_date")).to_have_value("2027-05-31")
        expect(page.locator("#f-term_months")).to_have_value("24")
        expect(page.locator("#f-pay_frequency")).to_have_value("fortnightly")
        expect(page.locator("#f-pretax_payment")).to_have_value("500.00")
        expect(page.locator("#f-fbt_base_value")).to_have_value("61000.00")
        expect(page.locator("#f-package_name")).to_have_value("Example Packaging")
        expect(page.locator("#f-estimated_km_per_year")).to_have_value("15000")
        expect(page.locator("#f-associate_lease")).to_be_checked()               # the form is an associate lease form
        expect(page.locator("#f-financier")).to_be_hidden()
        if folder is None:
            page.get_by_role("button", name="Add lease").click()
            page.wait_for_url(re.compile(r"msg=lease_added"))
            expect(page.locator(".lease")).to_contain_text("Example Packaging")
            raise Skip("the invoice folder isn't on in this stack, so the lease documents weren't saved")
        page.set_input_files("#f-agreement_file", {"name": "browser-test-lease-agreement.pdf", "mimeType": "application/pdf",
                                                   "buffer": readable_pdf(MARKER + " lease agreement")})
        page.get_by_role("button", name="Add lease").click()
        page.wait_for_url(re.compile(r"msg=lease_added"))
        docs = page.locator(".lease-documents")
        expect(docs.locator("li[data-document=form] a")).to_be_visible()
        expect(docs.locator("li[data-document=agreement] a")).to_be_visible()
        for doc in ("form", "agreement"):
            href = docs.locator(f"li[data-document={doc}] a").get_attribute("href")
            resp = page.request.get(UI.rstrip("/") + href[len(BASE):])
            assert resp.status == 200 and resp.headers["content-type"] == "application/pdf", f"{doc}: HTTP {resp.status}"
        shot(page, "lease-documents-phone")
    finally:
        remove_marked(page)


def test_lease_periods(page):
    """Two lease periods on one car, one after the other: the one that isn't current is folded away under
    "Other lease periods"."""
    try:
        car = add_marked_car(page)
        open_add_lease(page, car)
        fill_basic_lease(page, "2023-03-15", 24, financier="Example Finance")
        page.get_by_role("button", name="Add lease").click()
        page.wait_for_url(re.compile(r"msg=lease_added"))
        page.click("#add-lease-top")
        fill_basic_lease(page, "2025-06-01", 24, financier="Second Finance")
        page.get_by_role("button", name="Add lease").click()
        page.wait_for_url(re.compile(r"msg=lease_added"))
        expect(page.locator("section.lease")).to_have_count(2)
        expect(page.locator("h2.lease-others")).to_have_text("Other lease periods")
        period = page.locator("details.lease-period")
        expect(period).to_have_count(1)
        assert period.get_attribute("open") is None, "the other period should be folded"
        expect(period.locator("summary")).to_contain_text("15 Mar 2023 to 14 Mar 2025")
        expect(page.locator("#lease-form")).to_have_count(0)
        expect(page.locator(".lease").first).to_contain_text("Second Finance")      # the current one is on show
        expect(period.locator(".lease")).to_be_hidden()
        period.locator("summary").click()
        expect(period.locator(".lease")).to_be_visible()
        expect(period.locator(".lease")).to_contain_text("Example Finance")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
        shot(page, "lease-periods-phone")
    finally:
        remove_marked(page)


def test_fbt_report(page):
    try:
        fbt_checks(page)
    finally:
        remove_marked(page)       # the expense, then the car, even when a check failed


def fbt_checks(page):
    """The per-FBT-year report for a car added by hand, with a lease and one expense: the current FBT year,
    the taxable value, the pool the expense falls in, and the year links. The car is deleted at the end."""
    from datetime import date, timedelta
    today = date.today()
    add_marked_car(page).get_by_role("link", name="Lease").click()
    expect(page.locator("#no-lease")).to_be_visible()
    page.get_by_role("link", name="Costs, taxable value and contributions by FBT year").click()
    expect(page.locator("#fbt-nolease")).to_be_visible()          # no lease yet: a helpful message, not an error
    page.go_back()
    page.click("#add-lease-top")
    page.locator("#f-financier").fill("Example Finance")
    page.get_by_label("Lease start").fill((today - timedelta(days=60)).isoformat())
    page.locator("#f-end_date").fill((today + timedelta(days=1035)).isoformat())
    page.get_by_label("Term (months)").fill("36")
    page.get_by_label("Paid").select_option("fortnightly")
    page.get_by_label("Pre-tax part, per pay").fill("300")
    page.get_by_label("Post-tax part, per pay").fill("20")
    page.get_by_label("FBT base value").fill("62000")
    page.locator("#f-estimated_km_per_year").fill("15000")
    page.get_by_role("button", name="Add lease").click()
    page.wait_for_url(re.compile(r"msg=lease_added"))
    expect(page.locator("#lease-km-pace")).to_contain_text("not known")       # no odometer reading yet: not 0
    page.goto(UI + "new")                                        # an expense in the Insurance pool, today
    page.select_option("select[name=car_id]", label=MARKER)
    page.select_option("select[name=category_id]", label="Insurance")
    page.fill("input[name=amount]", "321.00")
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    page.goto(UI + "cars")
    page.locator(".car", has_text=MARKER).get_by_role("link", name="Lease").click()
    page.get_by_role("link", name="Costs, taxable value and contributions by FBT year").click()
    year = today.year if today.month >= 4 else today.year - 1
    expect(page.locator("#fbt-year")).to_have_text(f"FBT {year}–{(year + 1) % 100:02d}")
    body = page.inner_text("body")
    for shown in ("Taxable value", "Contribution required", "Expected by the end of the year", "$321.00",
                  "not tax advice", "Running costs"):
        assert shown in body, f"{shown!r} is not on the FBT report"
    expect(page.locator("#fbt-pools")).to_have_count(0)                  # budgets are off: costs by category only
    expect(page.locator("#fbt-categories tr", has_text="Insurance")).to_contain_text("$321.00")
    expect(page.locator("#purchases")).to_contain_text("No purchases")
    expect(page.locator("#pool-total")).to_contain_text("$321.00")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    shot(page, "fbt-phone")
    km_pace_checks(page, today, year)
    page.goto(f"{page.url}?year={year - 3}")                        # a year before the lease: nothing held, still a page
    expect(page.locator("#fbt-notheld")).to_be_visible()
    assert page.get_by_role("link", name=re.compile(r"FBT \d{4}–\d\d →")).count() == 1, "no link on to a year with data"


def this_income_year():
    from datetime import date
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


def set_tax_year(page, bracket, medicare="not_exempt"):
    """Settings > Cost after tax: this income year's bracket (by its first dollar; "" follows the year before) and
    Medicare levy status. -> what they were before."""
    year = this_income_year()
    page.goto(UI + "settings")
    pick, levy = (page.locator(f"select[name=tax_employee_{year}_{part}]") for part in ("bracket", "medicare"))
    before = (pick.input_value(), levy.input_value())
    pick.select_option(bracket)
    levy.select_option(medicare)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=settings"))
    return before


def after_tax_dashboard_checks(page):
    """With a rate and a lease the dashboard has both after-tax panels, and they sit below the Kilometre pace row
    without covering it (a tall viewport: Grafana only draws the panels in view)."""
    car_id = re.search(r"/cars/(\d+)/", page.url).group(1)
    wide = page.context.browser.new_context(viewport={"width": 1280, "height": 3600})   # down to the after-tax TCO row
    graf = wide.new_page()
    try:
        for _ in range(12):      # Grafana re-reads the provisioned dashboard every 60 seconds
            graf.goto(GRAFANA + f"d/garagebooks-tco/cost-of-ownership?var-car_id={car_id}")
            graf.wait_for_load_state("networkidle")
            graf.wait_for_timeout(2000)
            if graf.get_by_text("After tax by FBT year", exact=True).count():
                break
            graf.wait_for_timeout(10000)
        shot(graf, "after-tax-dashboard")
        expect(graf.get_by_text("Running costs after tax", exact=True).first).to_be_visible(timeout=30000)
        expect(graf.get_by_text("After tax by FBT year", exact=True).first).to_be_visible()
        for title in ("After-tax cost by income year", "Household cost after tax", "Lease after tax by income year",
                      "After tax by category and FBT year"):
            expect(graf.get_by_text(title, exact=True).first).to_be_visible()
    finally:
        wide.close()


def test_cost_after_tax(page):
    """This income year's tax bracket in Settings, "Paid from" on the form, the after-tax figure in the list and the
    After tax part of the FBT year report. The year's choice is put back as it was, and the car (and its expense)
    deleted, at the end."""
    before = set_tax_year(page, "45001")                 # $45,001 - $135,000 (30%), not exempt: 32%
    try:
        cost_after_tax_checks(page)
    finally:
        remove_marked(page)
        set_tax_year(page, *before)


def cost_after_tax_checks(page):
    from datetime import date, timedelta
    today = date.today()
    year = this_income_year()
    assert page.input_value(f"select[name=tax_employee_{year}_bracket]") == "45001", "the bracket wasn't kept"
    expect(page.locator("#tax-years")).to_contain_text("$45,001 – $135,000 (30%)")
    expect(page.locator("#ev-rates")).to_contain_text("FBT 2026–27")        # the ATO's rates are listed, as data
    expect(page.locator("#ev-rates input")).to_have_count(0)                # and not edited here
    shot(page, "settings-after-tax")
    add_marked_car(page).get_by_role("link", name="Lease").click()
    page.click("#add-lease-top")
    page.locator("#f-financier").fill("Example Finance")
    page.get_by_label("Lease start").fill((today - timedelta(days=60)).isoformat())
    page.locator("#f-end_date").fill((today + timedelta(days=1035)).isoformat())
    page.get_by_label("Term (months)").fill("36")
    page.get_by_label("Paid").select_option("fortnightly")
    page.get_by_label("Pre-tax part, per pay").fill("300")
    page.get_by_label("Post-tax part, per pay").fill("20")
    page.get_by_label("FBT base value").fill("62000")
    page.locator("#f-charging_reimbursed").check()                         # the marked car is battery electric
    page.get_by_role("button", name="Add lease").click()
    page.wait_for_url(re.compile(r"msg=lease_added"))
    page.goto(UI + "new")
    page.select_option("select[name=car_id]", label=MARKER)
    expect(page.locator("#paid-from-field")).to_be_visible()               # the car has a lease
    page.select_option("select[name=category_id]", label="Insurance")
    page.fill("input[name=amount]", "1100.00")
    page.fill("input[name=vendor]", MARKER)
    shot(page, "form-paid-from")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    row = page.locator("tr", has_text=MARKER)
    expect(row.locator("small.after-tax")).to_contain_text("$700.00 after tax")
    expect(page.locator("#after-tax-total")).to_contain_text("after tax")
    shot(page, "list-after-tax")
    row.get_by_role("link", name="Edit").click()                           # paid personally: no after-tax figure
    page.select_option("select[name=packaged]", "personal")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=updated"))
    expect(page.locator("tr", has_text=MARKER).locator("small.after-tax")).to_have_count(0)
    page.locator("tr", has_text=MARKER).get_by_role("link", name="Edit").click()
    page.select_option("select[name=packaged]", "auto")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=updated"))
    page.goto(UI + "cars")
    page.locator(".car", has_text=MARKER).get_by_role("link", name="Lease").click()
    page.get_by_role("link", name="Costs, taxable value and contributions by FBT year").click()
    after = page.locator("#fbt-after-tax")
    expect(after.locator("#fbt-after-tax-running")).to_contain_text("$700.00")
    expect(after.locator("#fbt-reimbursement")).to_contain_text("Not known")     # no odometer: never counted as nothing
    expect(after.locator("#fbt-after-tax-total")).to_contain_text("unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    shot(page, "fbt-after-tax")
    after_tax_dashboard_checks(page)
    # (With no year chosen at all the pages say so and show nothing: tests/test_after_tax.py. The dev stack may hold
    # earlier years' choices, which a later year follows, so it isn't tried here.)


def km_pace_checks(page, today, year):
    """On the FBT report of the car fbt_checks made (lease from 60 days ago, 15,000 km a year): with no reading at the
    start the figures that need one say not known; a Purchase expense the day before the lease, with an odometer, gives
    the start reading, and the figures follow. Then the lease page's short version."""
    from datetime import timedelta
    report = page.url
    section = page.locator("#km-pace")
    expect(section).to_be_visible()
    expect(section.locator('[data-figure="expected"]')).to_contain_text("km")
    expect(section.locator('[data-figure="start-odometer"]')).to_contain_text("not known")
    expect(section.locator('[data-figure="driven"]')).to_contain_text("not known")
    page.goto(UI + "new")
    page.select_option("select[name=car_id]", label=MARKER)
    page.select_option("select[name=category_id]", label="Initial purchase")
    page.fill("input[name=date]", (today - timedelta(days=61)).isoformat())
    page.fill("input[name=amount]", "1.00")
    page.fill("input[name=odometer_km]", "10000")
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))
    page.goto(report)
    expect(section.locator('[data-figure="start-odometer"]')).to_have_text("10,000 km")
    expect(section.locator('[data-figure="driven"]')).to_have_text("0 km")
    assert "not known" not in section.inner_text(), "a kilometre pace figure is still not known"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    shot(page, "km-pace-phone")
    page.goto(report.split("/fbt")[0] + "/lease")
    expect(page.locator("#lease-km-pace")).to_contain_text("0 km driven so far")
    shot(page, "km-pace-lease-phone")
    car_id = re.search(r"/cars/(\d+)/", report).group(1)
    wide = page.context.browser.new_context(viewport={"width": 1280, "height": 3600})   # Grafana only draws the panels in view
    graf = wide.new_page()
    for _ in range(12):      # Grafana re-reads the provisioned dashboard every 60 seconds: wait for the new panel
        graf.goto(GRAFANA + f"d/garagebooks-tco/cost-of-ownership?var-car_id={car_id}")
        graf.wait_for_load_state("networkidle")
        graf.wait_for_timeout(2000)
        if graf.get_by_text("Km per day needed from today", exact=True).count():
            break
        graf.wait_for_timeout(10000)
    shot(graf, "km-pace-dashboard")
    expect(graf.get_by_text("Kilometre pace", exact=True).first).to_be_visible(timeout=30000)
    expect(graf.get_by_text("Expected km", exact=True).first).to_be_visible()
    expect(graf.get_by_text("Driven so far", exact=True).first).to_be_visible()
    wide.close()
    page.goto(report)                    # back where the caller was


def test_reminders(page):
    try:
        reminder_checks(page)
    finally:
        remove_marked(page)       # takes the car, its reminders and the test expense with it


def reminder_checks(page):
    """A car added by hand with an odometer reading: add a repeating reminder that is nearly due, see it
    on the page, the nav and the dashboard, resolve it from the expense that did the job, see the next one
    made, then reopen it."""
    title = f"{MARKER} tyre rotation"
    page.goto(UI + "cars")
    page.fill("#add-car input[name=name]", MARKER)
    page.click("#add-car button[type=submit]")
    page.wait_for_url(re.compile(r"msg=car_added"))
    car = page.locator(".car", has_text=MARKER)
    car_id = car.get_attribute("id").removeprefix("car-")
    from datetime import date, timedelta
    page.goto(UI + "new")                                                             # the odometer comes from an expense
    page.select_option("select[name=car_id]", label=MARKER)
    page.wait_for_load_state("networkidle")
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=date]", (date.today() - timedelta(days=3)).isoformat())     # before the expense below
    page.fill("input[name=amount]", "40")
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=odometer_km]", "12,345")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))

    page.goto(UI + "reminders")
    page.select_option("#add-reminder select[name=car_id]", label=MARKER)
    standard = page.locator("#standard-reminder select")                              # standard reminders, by the car's type
    expect(standard).to_be_visible()
    assert page.locator("#standard-reminder option", has_text="Engine oil").evaluate("o => o.disabled || o.hidden"), \
        "a battery electric car shouldn't be offered an engine oil change"
    standard.select_option(label="Tyre rotation")
    expect(page.get_by_label("What is it for?")).to_have_value("Tyre rotation")
    expect(page.get_by_label("Every (km)")).to_have_value("10000")
    shot(page, "reminders-add-phone")
    page.get_by_label("What is it for?").fill(title)
    expect(page.get_by_label("Category")).to_have_value(                             # the standard one chose both
        page.locator("#f-category_id option", has_text="Service").get_attribute("value"))
    expect(page.locator("#f-service_item_id option:checked")).to_have_text("Tyre rotation")
    page.get_by_label("At this odometer reading (km)").fill("12800")
    page.get_by_label("Every (km)").fill("10000")
    page.get_by_label("Warn me this many days before").fill("-3")
    page.get_by_role("button", name="Add reminder").click()
    expect(page.locator("#e-lead_days")).to_contain_text("whole number")             # the error is beside the field...
    assert page.get_by_label("What is it for?").input_value() == title               # ...and nothing typed is lost
    page.get_by_label("Warn me this many days before").fill("30")
    page.get_by_role("button", name="Add reminder").click()
    page.wait_for_url(re.compile(r"msg=reminder_added"))
    item = page.locator(".reminder", has_text=title).first
    expect(item).to_have_attribute("data-status", "due_soon")
    expect(item).to_contain_text("Due soon")                                          # a word, not only a colour
    expect(item).to_contain_text("455 km to go")
    expect(item).to_contain_text("12,345 km")                                         # the car's odometer, and where it is from
    expect(item).to_contain_text("entered")
    expect(page.locator("header nav a", has_text="Reminders").locator(".count")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
    shot(page, "reminders-phone")
    page.goto(UI)
    expect(page.locator("#reminder-notice")).to_contain_text("due soon")

    wide = page.context.browser.new_context(viewport={"width": 1280, "height": 2200})   # Grafana only draws the panels in view
    graf = wide.new_page()
    graf.goto(GRAFANA + f"d/garagebooks-tco/cost-of-ownership?var-car_id={car_id}")
    graf.wait_for_load_state("networkidle")
    graf.wait_for_timeout(2000)
    shot(graf, "reminders-dashboard")
    expect(graf.get_by_text(title).first).to_be_visible(timeout=30000)
    expect(graf.get_by_text("Due soon", exact=True).first).to_be_visible()
    wide.close()

    page.goto(UI + "new")                                                             # the job is done: an expense for it
    page.select_option("select[name=car_id]", label=MARKER)
    page.select_option("select[name=category_id]", label="Service")
    page.locator("#service-fields label", has_text="Tyre rotation").locator("input").check()
    page.fill("input[name=amount]", "90")
    page.fill("input[name=vendor]", MARKER)
    page.fill("input[name=odometer_km]", "12,820")
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"/reminders\?msg=saved"))                           # the car has an open reminder: offered
    expect(page.get_by_label(re.compile(title))).to_be_checked()                      # its service item is ticked
    shot(page, "reminders-offer-phone")
    page.get_by_role("button", name="Resolve ticked").click()
    page.wait_for_url(re.compile(r"msg=reminder_resolved_next"))
    items = page.locator(".reminder[data-status=upcoming]", has_text=title)
    expect(items).to_have_count(1)                                                    # the next one: 10,000 km on from 12,820
    expect(items.first).to_contain_text("22,820 km")
    resolved = page.locator("#resolved .reminder", has_text=title)
    expect(resolved).to_have_count(1)
    expect(resolved).to_contain_text(MARKER)                                          # the expense that did it
    shot(page, "reminders-resolved-phone")

    page.once("dialog", lambda d: d.accept())                                         # resolved by mistake: reopen it
    resolved.get_by_role("button", name="Reopen").click()
    page.wait_for_url(re.compile(r"msg=reminder_reopened"))
    expect(page.locator(".reminder[data-status=upcoming]", has_text=title)).to_have_count(0)   # the next one went again
    expect(page.locator(".reminder[data-status=overdue]", has_text=title)).to_have_count(1)    # 12,820 is past 12,800
    expect(page.locator("#resolved .reminder", has_text=title)).to_have_count(0)


def test_add_form_runs_without_the_teslamate_button(page):
    """The Fill from TeslaMate button isn't drawn without a car TeslaMate tracks (or while TeslaMate isn't usable).
    The form's script must carry on past that: reading a chosen PDF and the duplicate warning come after it."""
    other = page.context.new_page()
    errors = []
    other.on("pageerror", lambda e: errors.append(str(e)))
    other.add_init_script("new MutationObserver(() => { const b = document.getElementById('fill-odometer'); if (b) b.remove(); })"
                          ".observe(document, {childList: true, subtree: true});")
    try:
        other.goto(UI + "new")
        other.wait_for_load_state("networkidle")
        expect(other.locator("#fill-odometer")).to_have_count(0)
        assert not errors, errors
    finally:
        other.close()


def test_fuel_receipt(page):
    """A service station receipt fills the Add form, litres and price sit before the odometer, Use my location fills
    the location (the browser's position is faked: this stack is plain HTTP), and all of it is kept."""
    receipt = readable_pdf("SHELL PUMP #1 PREMIUM 95) Tj 0 -30 Td (40.00L @ $2.000/L 80.00) Tj 0 -30 Td (TOTAL $77.00) "
                           "Tj 0 -30 Td (05/03/26 07:45")
    pdf = [{"name": "receipt.pdf", "mimeType": "application/pdf", "buffer": receipt}]
    page.goto(UI + "cars")
    page.fill("#add-car input[name=name]", MARKER)
    page.select_option("#add-car select[name=kind]", "ICE")
    page.click("#add-car button[type=submit]")
    page.wait_for_url(re.compile(r"msg=car_added"))
    other = page.context.new_page()
    other.add_init_script(
        "Object.defineProperty(window, 'isSecureContext', {get: () => true});"
        "Object.defineProperty(navigator, 'geolocation', {value: {getCurrentPosition: (ok) =>"
        " ok({coords: {latitude: -30.123456, longitude: 150.654321, accuracy: 8}})}});")
    try:
        other.goto(UI + "new")
        electric = other.locator("select[name=car_id] option[data-kind=BEV]")
        if electric.count():                      # an electric car doesn't take fuel: the category stays unchosen
            other.select_option("select[name=car_id]", electric.first.get_attribute("value"))
            other.set_input_files("#invoice-file", files=pdf)
            expect(other.locator("#prefill-hint")).to_contain_text("doesn't take fuel")
            expect(other.locator("select[name=category_id]")).to_have_value("")
            other.goto(UI + "new")
        other.select_option("select[name=car_id]", label=MARKER)
        other.set_input_files("#invoice-file", files=pdf)
        expect(other.locator("input[name=litres]")).to_have_value("40.00")
        expect(other.locator("input[name=unit_price]")).to_have_value("2.000")
        expect(other.locator("input[name=amount]")).to_have_value("77.00")
        expect(other.locator("input[name=date]")).to_have_value("2026-03-05")
        expect(other.locator("input[name=time]")).to_have_value("07:45")
        expect(other.locator("select[name=category_id] option:checked")).to_have_text("Fuel")
        # This receipt has no address, so nothing is looked up and the current location is offered instead.
        expect(other.locator("#location-hint")).to_contain_text("Use my location")
        top = lambda name: other.locator(f"input[name={name}]").bounding_box()["y"]  # noqa: E731 - used twice, right here
        assert top("litres") < top("odometer_km") < top("location")
        other.click("#use-location")
        expect(other.locator("input[name=location]")).to_have_value("-30.123456, 150.654321")
        other.fill("input[name=odometer_km]", "61000")
        expect(other.locator("#fuel-summary")).not_to_be_empty()
        shot(other, "fuel-receipt-form")
        other.fill("input[name=vendor]", MARKER)
        other.fill("input[name=invoice_url]", INVOICE)
        other.locator("input[name=invoice_file]").set_input_files([])
        other.click("button[type=submit].button")
        other.wait_for_url(re.compile(r"msg=saved"))
        other.goto(UI)
        other.locator(f"tr:has-text('{MARKER}') a:has-text('Edit')").first.click()
        expect(other.locator("input[name=location]")).to_have_value("-30.123456, 150.654321")
        expect(other.locator("input[name=unit_price]")).to_have_value("2.000")
    finally:
        other.close()
        remove_marked(page)                       # the expense, then the car added for it


def test_dashboard(page):
    add_expense(page, "62000", "Initial purchase")
    add_expense(page, "1500", "Tyres")
    errors = []
    page.on("response", lambda r: errors.append(f"{r.status} {r.url}")
            if "/api/ds/query" in r.url and r.status >= 400 else None)
    page.set_viewport_size({"width": 1600, "height": 2800})
    page.goto(GRAFANA + "d/garagebooks-tco/cost-of-ownership")
    # Stat titles appear, and a value comes back from the datasource.
    expect(page.get_by_text("Total cost", exact=True)).to_be_visible(timeout=30000)
    expect(page.get_by_text("Cost per km — running")).to_be_visible()
    expect(page.get_by_text("Running costs per month")).to_be_visible()
    expect(page.get_by_text("Running costs per FBT year")).to_be_visible()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    shot(page, "dashboard")
    assert not errors, errors
    assert page.locator("[data-testid='data-testid Panel status error']").count() == 0, "a panel shows an error"
    body = page.inner_text("body")
    assert "Reminders" in body, "the Reminders panel is missing"
    assert "No data" not in body.split("Reminders")[0], "a headline panel has no data"
    assert MARKER in body, "expenses table did not list the test row"
    assert "$62,000.00" in body, "amounts must show in full, grouped, not rescaled"
    assert " Mm" not in body and not re.search(r"\$\d+(\.\d+)?K", body), "units were auto-scaled"
    table_link = page.locator(f"a[href='{INVOICE}']").first
    expect(table_link).to_be_visible()


def test_sold_panel(page, dash_page):
    """The dashboard has a "Sold" panel only once some car is sold: the app rewrites the dashboard when a sale
    is saved, and Grafana re-reads its provisioned files on an interval, so it can take a little while."""
    page.goto(UI + "cars")
    if page.locator(".car-sold").count():
        raise Skip("a car in this stack is already sold")
    dash_page.set_viewport_size({"width": 1600, "height": 2800})
    url = GRAFANA + "d/garagebooks-tco/cost-of-ownership"
    dash_page.goto(url)
    expect(dash_page.get_by_text("Total cost", exact=True)).to_be_visible(timeout=30000)
    dash_page.wait_for_timeout(2000)
    assert dash_page.get_by_text("Sold", exact=True).count() == 0, "the Sold panel shows with no car sold"
    try:
        car = add_marked_car(page)
        car.locator("summary", has_text="Sold this car?").click()
        car.locator(".car-sale input[name=date]").fill("2020-01-02")
        car.locator(".car-sale input[name=price]").fill("7500")
        car.locator(".car-sale input[name=km]").fill("13000")
        car.get_by_role("button", name="Save sale").click()
        page.wait_for_url(re.compile(r"msg=car_sold"))
        appeared = False
        for _ in range(12):                              # Grafana looks at the file again every few seconds
            dash_page.wait_for_timeout(8000)
            dash_page.goto(url)
            try:
                expect(dash_page.get_by_text("Sold", exact=True).first).to_be_visible(timeout=8000)
                appeared = True
                break
            except AssertionError:
                continue
        assert appeared, "the Sold panel didn't appear within two minutes of a sale"
        shot(dash_page, "dashboard-sold")
    finally:
        page.goto(UI + "cars")                           # un-sell it, so the dashboard goes back to having no Sold panel
        sold = page.locator(".car", has_text=MARKER)
        if sold.locator(".car-sold").count():
            sold.locator("summary", has_text="Sale").click()
            sold.get_by_role("button", name="Clear sale").click()
            page.wait_for_url(re.compile(r"msg=car_unsold"))
        remove_marked(page)


# The stand-in claims connector (tests/fake_connector.py): the dev stack's own service, never a real provider.
FAKE_CONNECTOR = "http://dev-claims-connector:8765"


def fake_connector(page, config=None):
    """Reset the stand-in connector, then change what it answers. Skips when this stack has none."""
    import json
    try:
        assert page.request.post(FAKE_CONNECTOR + "/_fake/reset").ok
        if config:
            assert page.request.post(FAKE_CONNECTOR + "/_fake/config", data=json.dumps(config),
                                     headers={"Content-Type": "application/json"}).ok
    except Exception as exc:
        raise Skip(f"no stand-in claims connector on this stack ({str(exc)[:80]})") from exc


def add_claimable_expense(page, today, amount="1200.00"):
    """A marked car with a lease covering today, and a tyres expense on it: one the Claim link is offered for."""
    from datetime import timedelta
    car = add_marked_car(page)
    car.get_by_role("link", name="Lease").click()
    page.click("#add-lease-top")
    page.locator("#f-financier").fill("Example Finance")
    page.get_by_label("Lease start").fill((today - timedelta(days=30)).isoformat())
    page.locator("#f-end_date").fill((today + timedelta(days=1065)).isoformat())
    page.get_by_label("Term (months)").fill("36")
    page.get_by_label("Paid").select_option("fortnightly")
    page.get_by_label("Pre-tax part, per pay").fill("300")
    page.get_by_role("button", name="Add lease").click()
    page.wait_for_url(re.compile(r"msg=lease_added"))
    page.goto(UI + "new")
    page.select_option("select[name=car_id]", label=MARKER)
    page.select_option("select[name=category_id]", label="Tyres")
    page.fill("input[name=amount]", amount)
    page.fill("input[name=vendor]", MARKER)
    page.click("button[type=submit].button")
    page.wait_for_url(re.compile(r"msg=saved"))


def test_list_with_a_claim_link_fits_a_phone(page):
    """Claiming is on and a lease covers the expense, so the row has a Claim link: it must not push the amount or
    the Edit link off a phone's screen, in tiles or in rows (the link has a cell of its own)."""
    from datetime import date
    fake_connector(page)
    try:
        add_claimable_expense(page, date.today(), "1234.56")
        page.goto(UI)
        row = page.locator(f"tr:has-text('{MARKER}')")
        expect(row.locator("a.claim-link")).to_have_count(1)
        width = page.viewport_size["width"]
        for choice in ("tiles", "rows"):
            page.click(f"[data-view-choice={choice}]")
            if choice == "tiles":
                for text in ("$1,234.56", "Edit", "Claim"):
                    box = row.get_by_text(text, exact=False).first.bounding_box()
                    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, f"{text} is off-screen in tiles: {box}"
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                shot(page, "list-phone-claim")
            else:
                cut = page.evaluate("""marker => [...document.querySelectorAll('tbody tr')].filter(r => r.textContent.includes(marker))
                    .flatMap(r => [...r.querySelectorAll('td.date, td.cat .chip, td.amount, td.actions, td.claim-state')])
                    .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className + ': ' + e.textContent.trim())""", MARKER)
                assert cut == [], f"cut short in rows: {cut}"
        page.evaluate("localStorage.clear()")
    finally:
        remove_marked(page)


def test_real_onedrive_claim(page):
    """An expense whose invoice is in the real OneDrive (the test folder only) is claimed with that file: the claim
    page downloads it and previews through the stand-in connector, rather than asking for the file. Nothing is
    submitted. The car, lease and expenses are removed at the end; the uploaded PDF stays in the test folder."""
    from datetime import date
    page.goto(UI + "storage")
    if not page.locator("button:has-text('Disconnect')").count():
        raise Skip("dev stack isn't connected to OneDrive (scripts/dev.sh onedrive-link)")
    if not in_test_folder(page):
        raise Skip(f"cars aren't pointed at /{TEST_FOLDER}, so nothing was written")
    fake_connector(page)
    try:
        page.goto(UI + "new")                 # uploaded through a car whose folder is the test folder
        page.set_input_files("input[name=invoice_file]", {
            "name": "browser-test-claim.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF\n"})
        page.wait_for_load_state("networkidle")
        page.select_option("select[name=category_id]", label="Tyres")
        page.fill("input[name=amount]", "371.94")
        page.fill("input[name=vendor]", MARKER)
        page.fill("input[name=invoice_url]", "")
        page.click("button[type=submit].button")
        page.wait_for_url(re.compile(r"msg=saved"), timeout=60000)
        page.goto(UI)
        link = page.locator("tbody tr", has_text=MARKER).locator("a[href^='https://']").first.get_attribute("href")
        assert "onedrive" in link or "1drv" in link, f"not a OneDrive address: {link[:40]}"

        add_claimable_expense(page, date.today())
        page.goto(UI)
        row = page.locator("tbody tr", has_text=MARKER).filter(has_text="$1,200.00")
        row.locator("a:has-text('Edit')").first.click()
        page.fill("input[name=invoice_url]", link)
        page.click("button[type=submit].button")
        page.wait_for_url(re.compile(r"msg=updated"))
        page.goto(UI)
        page.locator("tbody tr", has_text=MARKER).filter(has_text="$1,200.00").locator("a.claim-link").click()
        expect(page.locator("#claim-preview")).to_contain_text("Claim $1200.00 for Tyres", timeout=60000)
        body = page.locator("main").inner_text()
        assert "only a link" not in body and "No invoice is attached" not in body, "the OneDrive invoice wasn't sent"
        expect(page.get_by_role("button", name="Submit claim")).to_be_visible()
        shot(page, "claim-preview-onedrive")
    finally:
        remove_marked(page)


def sync_provider_history(page):
    """Press Sync provider history: it starts in the background, the page shows it running and reloads itself when done."""
    page.get_by_role("button", name="Sync provider history").click()
    page.wait_for_url(re.compile(r"msg=claims_sync_started"))
    expect(page.locator("#sync-running")).to_have_count(0, timeout=40000)


def test_claim_renewal(page):
    """A registration sent as a renewal request through the stand-in connector: the choice of who pays, the notice's
    figures in view, the payment references following the category on the form, and the declaration to accept."""
    from datetime import date
    fake_connector(page)
    try:
        add_claimable_expense(page, date.today())
        page.goto(UI)
        page.locator("tbody tr", has_text=MARKER).get_by_role("link", name="Edit").click()
        refs = page.locator("details.payment-refs")
        expect(refs).to_be_hidden()                                       # tyres have none
        page.select_option("select[name=category_id]", label="Registration")
        expect(refs).to_be_visible()
        refs.locator("summary").click()
        page.fill("input[name=card_payment_ref]", "1111 2222 33")
        page.fill("input[name=bpay_biller_code]", "12345")
        page.fill("input[name=bpay_ref]", "9876 5432")
        page.click("button[type=submit].button")
        page.wait_for_url(re.compile(r"msg=updated"))
        page.goto(UI)
        page.locator("tbody tr", has_text=MARKER).locator("a.claim-link").click()
        expect(page.locator("#claim-method")).to_be_visible()
        page.get_by_role("link", name="Ask the provider to pay it").click()
        expect(page.get_by_label("BPAY biller code")).to_have_value("12345")
        expect(page.locator("#claim-declaration")).to_contain_text("Example declaration")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
        shot(page, "claim-renewal")
        page.get_by_role("button", name="Check again").click()            # without accepting: only checking
        expect(page.locator("#claim-declaration")).to_be_visible()
        page.get_by_label("I accept the declaration above").check()
        page.get_by_role("button", name="Send renewal request").click()
        page.wait_for_url(re.compile(r"msg=renewal_requested"))
        expect(page.locator("tbody tr", has_text=MARKER).locator("a.claim-badge")).to_contain_text("Renewal requested")
        page.locator("tbody tr", has_text=MARKER).get_by_role("link", name="Edit").click()
        expect(page.locator("#claim-record")).to_contain_text("Renewal requested from")
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Remove claim record").click()
        page.wait_for_url(re.compile(r"msg=claim_forgotten"))
    finally:
        remove_marked(page)


def test_claims(page):
    """Claim an expense through the stand-in connector: preview, a field it asks for, the sign-in with its
    code, submit, the badge and the reference, refusing to delete a claimed expense, refreshing the status,
    and removing the record. The car, lease and expense are made here and removed at the end."""
    from datetime import date, timedelta
    fake_connector(page)
    today = date.today()
    try:
        add_claimable_expense(page, today)

        row = page.locator("tbody tr", has_text=MARKER)
        expect(row.locator("a.claim-link")).to_have_count(1)
        row.locator("a.claim-link").click()
        expect(page.locator("#claim-preview")).to_contain_text("Claim $1200.00 for Tyres")
        expect(page.locator("#claim-preview")).to_contain_text("$1,090.91")
        expect(page.get_by_text("No invoice is attached")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll on a phone"
        shot(page, "claim-preview")

        # The provider wants something more: the box has its own label, and the preview comes back with it.
        fake_connector(page, {"needs_fields": [{"key": "description", "label": "What was it for?", "type": "text"}]})
        page.reload()
        expect(page.get_by_label("What was it for?")).to_be_visible()
        expect(page.get_by_role("button", name="Submit claim")).to_have_count(0)
        shot(page, "claim-needs-field")
        page.get_by_label("What was it for?").fill("New tyres")
        page.get_by_role("button", name="Check again").click()
        expect(page.get_by_role("button", name="Submit claim")).to_be_visible()

        # Signed out: sign in on the page, with the code the provider sends.
        fake_connector(page, {"signed_in": False})
        page.goto(page.url.split("?")[0])
        expect(page.locator("#claim-signin")).to_be_visible()
        page.get_by_role("button", name="Sign in").click()
        code = page.locator("#claim-signin input[name=code]")
        expect(code).to_be_visible(timeout=15000)
        shot(page, "claim-signin")
        code.fill("123456")
        page.get_by_role("button", name="Send code").click()
        expect(page.get_by_role("button", name="Submit claim")).to_be_visible(timeout=30000)   # signed in, back on the preview

        page.get_by_role("button", name="Submit claim").click()
        page.wait_for_url(re.compile(r"msg=claimed"))
        expect(page.locator(".notice:not(#no-tax-rate)")).to_contain_text("REF0001")
        row = page.locator("tbody tr", has_text=MARKER)
        expect(row.locator("a.claim-badge")).to_contain_text("REF0001")
        expect(row.locator("a.claim-link")).to_have_count(0)
        shot(page, "claim-submitted")

        # A claimed expense isn't deleted by accident.
        row.get_by_role("link", name="Edit").click()
        expect(page.locator("#claim-record")).to_contain_text("REF0001")
        page.once("dialog", lambda d: d.accept())
        page.click("button.danger")
        expect(page.get_by_role("heading", name="This expense has been claimed")).to_be_visible()

        # Refresh: the provider says it is paid.
        fake_connector(page, {"signed_in": True, "statuses": {"P0001": {
            "provider_id": "P0001", "reference": "REF0001", "status": "Paid", "approved_amount": 1100.0,
            "paid_date": today.isoformat()}}})
        # (the reset above also forgot the stand-in's own counter, so P0001 is the only id it knows)
        page.goto(UI + "claims")
        expect(page.locator("tr[data-expense]", has_text="REF0001")).to_have_count(1)
        page.get_by_role("button", name="Refresh claim statuses").click()
        page.wait_for_url(re.compile(r"msg=claims_refreshed"))
        expect(page.locator("tr[data-expense]", has_text="REF0001")).to_contain_text("Paid")
        expect(page.locator("tr[data-expense]", has_text="REF0001")).to_contain_text("$1,100.00")

        # The ledger: the provider's status, its history synced into the database and lined up with the expenses.
        fake_connector(page, {"signed_in": True, "transactions": [
            {"provider_id": "T1", "date": today.isoformat(), "amount": 600.0, "gst": 0, "type": "Reimbursement",
             "status": "Paid", "description": "Tyres", "payee": "Example Tyres Pty Ltd", "kind": "claim_payment",
             "car_id": None, "claim_provider_id": "P0001", "reference": "REF0001"},
            {"provider_id": "T2", "date": today.isoformat(), "amount": 500.0, "gst": 0, "type": "Reimbursement",
             "status": "Paid", "description": "Tyres", "payee": None, "kind": "claim_payment",
             "car_id": None, "claim_provider_id": "P0001", "reference": "REF0001"},
            {"provider_id": "K1", "date": today.isoformat(), "amount": 42.0, "gst": 0, "type": "Reimbursement",
             "status": "Paid", "description": "Home charging per km", "payee": None, "kind": "per_km_charging",
             "car_id": None, "claim_provider_id": None, "reference": None},
            {"provider_id": "T9", "date": today.isoformat(), "amount": 7.0, "gst": 0, "type": "Reimbursement",
             "status": "Paid", "description": "Example parking", "payee": None, "kind": "claim_payment",
             "car_id": None, "claim_provider_id": None, "reference": None}],
            "claims": [{"provider_id": "P0001", "reference": "REF0001", "line_id": "L1", "date": today.isoformat(),
                        "amount": 1200.0, "approved_amount": 1100.0, "status": "Approved",
                        "paid_date": today.isoformat(), "description": "Tyres", "car_id": None}]})
        page.goto(UI + "claims")
        expect(page.locator("#provider-status")).to_contain_text("Signed in")
        sync_provider_history(page)
        expect(page.locator("tr[data-tx=K1]")).to_contain_text("no expense needed")
        expect(page.locator("tr[data-tx=T9]")).to_contain_text("No matching expense")
        expect(page.locator("tr[data-tx=T1] a")).to_have_count(1)             # matched through its claim
        expect(page.locator("tr[data-tx=T2] a")).to_have_count(1)             # a part payment of the same claim
        expect(page.locator("tr[data-expense]", has_text="REF0001")).to_contain_text("Reimbursed in full")   # $600 + $500 of $1,100
        page.goto(UI + "claims?tx=unmatched")
        expect(page.locator("tr[data-tx]")).to_have_count(1)
        shot(page, "claims-page")

        # The record can be removed (the claim stays with the provider), after which it can be claimed again.
        page.goto(UI)
        page.locator("tbody tr", has_text=MARKER).get_by_role("link", name="Edit").click()
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Remove claim record").click()
        page.wait_for_url(re.compile(r"msg=claim_forgotten"))
        # The provider's own history still has that claim, which matches the expense: not offered again.
        expect(page.locator("tbody tr", has_text=MARKER).locator("a.claim-link")).to_have_count(0)
        # Once the provider's history no longer has it (the stand-in is reset), a sync frees the expense.
        fake_connector(page)
        page.goto(UI + "claims")
        sync_provider_history(page)
        page.goto(UI)
        expect(page.locator("tbody tr", has_text=MARKER).locator("a.claim-link")).to_have_count(1)
    finally:
        try:
            fake_connector(page)
        except Skip:
            pass
        remove_marked(page)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        phone = browser.new_context(**p.devices["Pixel 7"])
        page = phone.new_page()
        problems = []
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        remove_marked(page)

        step("form renders on a phone", test_form, page)
        step("odometer fills itself in", test_odometer_autofills, page)
        step("the Add form works without the Fill from TeslaMate button", test_add_form_runs_without_the_teslamate_button, page)
        step("fuel: a receipt fills the form, and the location is kept", test_fuel_receipt, page)
        step("odometer button uses the chosen date", test_odometer_button_for_past_date, page)
        step("a bad invoice link is explained, input kept", test_bad_link_is_explained, page)
        step("add, list, invoice link, edit, delete", test_add_list_edit_delete, page)
        step("delete can be cancelled", test_delete_can_be_cancelled, page)
        step("amount field works out sums", test_amount_calculator, page)
        step("payment calculator works out the true cost", test_payment_calculator, page)
        step("aCar import previews without saving", test_acar_import_preview, page)
        step("aCar import fits a phone and a desktop", test_acar_import_layout, page)
        step("wheel ticks show only where they apply", test_wheel_ticks, page)
        step("list: tiles or rows, sorting, and back to the same view", test_list_toggle_sort_and_return, page)
        step("rows on a phone show whole dates, categories and amounts", test_rows_on_a_phone_show_whole_values, page)
        step("no sideways scroll on a phone", test_phone_layout_has_no_sideways_scroll, page)
        step("categories: one Save, and a small delete button", test_categories_save_together_and_delete_is_a_small_button, page)
        step("cars: TeslaMate's are listed, one is added by hand, read and deleted", test_cars_page, page)
        step("cars: a car with no name is listed by make and model, bought and sold details", test_car_without_a_name, page)
        step("lease: add from a statement, see the figures, edit, delete", test_lease_page, page)
        step("lease: associate lease hides financier and residual, end follows the term", test_lease_associate, page)
        step("lease: a lease form PDF fills the boxes, documents are kept", test_lease_form_import, page)
        step("lease: several periods, the others folded", test_lease_periods, page)
        step("reminders: add, see them due, resolve from an expense, next one made, reopen",
             test_reminders, page)
        step("FBT year report: taxable value, costs by pool, year links", test_fbt_report, page)
        step("cost after tax: the rate, Paid from, the list and the FBT year report", test_cost_after_tax, page)
        step("header stays tidy on a phone", test_header_stays_tidy_on_a_phone, page)
        iphone = browser.new_context(**p.devices["iPhone 13"])
        step("header menu on an iPhone 13", test_header_stays_tidy_on_a_phone, iphone.new_page())
        step("phone list shows amount, invoice and edit", test_list_shows_key_fields_on_a_phone, page)
        step("a Claim link in the list still fits a phone", test_list_with_a_claim_link_fits_a_phone, page)
        step("claims: preview, needed field, sign-in code, submit, status refresh, remove record", test_claims, page)
        step("claims: a registration renewal request, its references and declaration", test_claim_renewal, page)
        step("settings: notifications address is kept secret, refused when bad, tested", test_notifications_settings, page)
        step("history page lists an add, an edit and a delete", test_history_page, page)
        step("a likely duplicate is asked about, then saved anyway", test_duplicate_is_asked_about_before_saving, page)
        step("the duplicate warning can be dismissed", test_duplicate_warning_can_be_dismissed, page)
        step("a PDF over 500 KB uploads (read-only container)", test_large_pdf_is_accepted, page)
        step("OneDrive page and upload field", test_onedrive_page, page)
        step("a PDF dropped on the form is chosen and read", test_pdf_can_be_dropped_on_the_form, page)
        step("file name settings start from the real defaults", test_settings_file_name_defaults_can_be_edited, page)
        step("Settings lists the scheduled jobs", test_settings_lists_the_scheduled_jobs, page)
        step("Connect OneDrive goes to Microsoft", test_connect_reaches_microsoft, page)
        step("folder browser (read-only)", test_folder_browser, page)
        step("real upload to the OneDrive test folder", test_real_upload, page)
        step("a claim sends an invoice kept in the real OneDrive", test_real_onedrive_claim, page)
        step("OneDrive file picker on the Add form", test_onedrive_picker, page)
        step("import page shows progress", test_import_page_shows_progress, page)
        # The built-in PDF viewer only exists in full Chromium, not the headless shell the rest uses.
        viewer = p.chromium.launch(channel="chromium")
        step("invoice folder: pick a folder, save a PDF, open it inline, pick it, import",
             test_local_folder, page, viewer.new_page())
        viewer.close()
        # Expected 400s from the deliberate bad submissions above aren't script errors.
        # (and the 409 of a claimed expense that the app refuses to delete)
        real = [t for t in problems if "400" not in t and "409" not in t]
        step("no script or CSP errors", lambda: (_ for _ in ()).throw(AssertionError(real)) if real else None)

        desktop = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
        dash_page = desktop.new_page()
        step("dashboard renders with data and invoice links", test_dashboard, dash_page)
        step("dashboard: the Sold panel appears once a car is sold", test_sold_panel, page, dash_page)
        step("history page at desktop width, light and dark", test_history_desktop, desktop.new_page())
        step("payment calculator on a desktop", test_payment_calculator_on_a_desktop, desktop.new_page())
        step("header is one row on a desktop", test_header_on_a_desktop, desktop.new_page())
        step("desktop list and form, light and dark", test_desktop_look, desktop.new_page())
        step("column widths can be dragged, are remembered, and reset", test_column_widths_can_be_dragged, desktop.new_page())
        step("footer links to the docs and names the build", test_footer_links_to_the_docs_and_names_the_build, desktop.new_page())
        remove_marked(page)
        browser.close()
    print(f"\n{passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    if not ONLY:     # a run of one step leaves the list of the last full (or rerun) run as it was
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            f.writelines(f"{name}\n" for name in failed)
    if failed:
        print("Run only these again: scripts/dev.sh test-browser --failed")
    sys.exit(1 if failed else 0)


main()
