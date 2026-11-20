"""Importing invoices from an Outlook.com mailbox folder per car.

The mailbox is signed in to with the same Entra app registration as OneDrive, as a separate sign-in
(Mail.Read only), and comes back through the same callback address. Each car can have a mailbox
folder; its emails are offered on the same import page as a OneDrive or server folder. A PDF attached
to an email is one row; an email with no PDF (a refund notice, say) is a row read from its text.

Microsoft is FakeOutlook (FakeMicrosoft plus the mail calls); PDF text is canned, as in test_import.
"""

import base64
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from garagebooks import invoices, notices
from tests.test_app import ORIGIN, rows
from tests.test_onedrive import (CLIENT_ID, FakeMicrosoft, account, an_hour_passes, app_for,  # noqa: F401  (fixtures)
                                 choose_folder, save_credentials, sign_in, start_sign_in)
from tests.test_validation import PDF

TYRES_PDF = b"%PDF-mail-tyres"
TEXT = {TYRES_PDF: "Invoice Date: 24/10/2026\n  Example Tyres Pty Ltd\n  Tyres\n  Total   $1,234.50\n", PDF: ""}

MAIL_ROOT = "ROOTMAIL"
MAIL_FOLDERS = {   # id -> (name, parent)
    "INBOX": ("Inbox", MAIL_ROOT),
    "CARMAIL": ("Test car invoices", "INBOX"),
}
WEBLINK = "https://outlook.live.com/owa/?ItemID={}&exvsurl=1&viewmodel=ReadMessageItem"


def message(mid, subject, sender, received, attachments=(), body=""):
    return {"id": mid, "subject": subject, "from": {"emailAddress": {"name": sender, "address": "billing@example.com"}},
            "receivedDateTime": received, "hasAttachments": bool(attachments), "webLink": WEBLINK.format(mid),
            "attachments": list(attachments), "_body": body}


def pdf_attachment(aid, name, content):
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": aid, "name": name,
            "contentType": "application/pdf", "size": len(content), "isInline": False, "_content": content}


MESSAGES = [
    message("M1", "Your tyre invoice", "Example Tyres", "2026-10-25T01:00:00Z",
            [pdf_attachment("A1", "INV12345.pdf", TYRES_PDF),
             {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A2", "name": "logo.png",
              "contentType": "image/png", "size": 10, "isInline": True}]),
    message("M2", "Refund processed for order INV12345", "Example Tyres", "2026-10-30T02:00:00Z",
            body="Hi,\n\nWe have refunded $45.00 to your card ending 1111.\n\nExample Tyres Pty Ltd\n"),
    message("M3", "Hello", "A Friend", "2026-11-01T03:00:00Z", body="Just saying hi.\n"),
]


class FakeOutlook(FakeMicrosoft):
    """FakeMicrosoft plus the Graph mail calls, and a refresh that remembers the scope asked for."""

    def __init__(self):
        super().__init__()
        self.messages = [dict(m) for m in MESSAGES]
        self.scopes = []

    def __call__(self, method, url, data=None, headers=None, raw=False):
        headers = headers or {}
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if path.endswith("/oauth2/v2.0/token"):
            self.scopes.append(parse_qs(data.decode()).get("scope", [""])[0])
            return super().__call__(method, url, data, headers, raw)
        if "/me/mailFolders" not in path and "/me/messages" not in path and not path.endswith("/me"):
            return super().__call__(method, url, data, headers, raw)
        self.calls.append((method, url, data, headers))
        assert headers["Authorization"].startswith("Bearer AT-")
        assert method == "GET", "the mailbox is only ever read"
        if path.endswith("/me"):
            return 200, {"displayName": "Test Owner", "mail": "owner@example.com"}
        if path.endswith("/mailFolders/msgfolderroot"):
            return 200, {"id": MAIL_ROOT, "displayName": "Top of Information Store"}
        m = re.search(r"/mailFolders/([^/]+)/childFolders$", path)
        if m:
            parent = MAIL_ROOT if m.group(1) == "msgfolderroot" else m.group(1)
            return 200, {"value": [{"id": k, "displayName": n, "parentFolderId": p}
                                   for k, (n, p) in MAIL_FOLDERS.items() if p == parent]}
        m = re.search(r"/mailFolders/([^/]+)/messages$", path)
        if m:
            assert m.group(1) == "CARMAIL"
            assert 'IdType="ImmutableId"' in headers.get("Prefer", "")
            return 200, {"value": [{k: v for k, v in msg.items() if not k.startswith("_")}
                                   | {"attachments": [{k: v for k, v in a.items() if not k.startswith("_")}
                                                      for a in msg["attachments"]]}
                                   for msg in self.messages]}
        m = re.search(r"/mailFolders/([^/]+)$", path)
        if m:
            if m.group(1) not in MAIL_FOLDERS:
                return 404, {"error": {"code": "ErrorItemNotFound", "message": "not found"}}
            name, parent = MAIL_FOLDERS[m.group(1)]
            return 200, {"id": m.group(1), "displayName": name, "parentFolderId": parent}
        m = re.search(r"/messages/([^/]+)/attachments/([^/]+)/\$value$", path)
        if m:
            assert raw
            msg = next(x for x in self.messages if x["id"] == m.group(1))
            return 200, next(a for a in msg["attachments"] if a["id"] == m.group(2))["_content"]
        m = re.search(r"/messages/([^/]+)/attachments/([^/]+)$", path)
        if m:
            msg = next(x for x in self.messages if x["id"] == m.group(1))
            a = next(a for a in msg["attachments"] if a["id"] == m.group(2))
            return 200, {k: v for k, v in a.items() if not k.startswith("_")}
        m = re.search(r"/messages/([^/]+)$", path)
        if m:
            assert 'outlook.body-content-type="text"' in headers.get("Prefer", "")
            msg = next(x for x in self.messages if x["id"] == m.group(1))
            return 200, {**{k: v for k, v in msg.items() if not k.startswith("_")},
                         "body": {"contentType": "text", "content": msg["_body"]}}
        raise AssertionError(f"unexpected mail call {method} {url}")


@pytest.fixture()
def ms():
    from tests.test_onedrive import CHILDREN, FOLDERS
    snapshot = ({k: dict(v) for k, v in FOLDERS.items()}, {k: list(v) for k, v in CHILDREN.items()})
    yield FakeOutlook()
    FOLDERS.clear(); FOLDERS.update(snapshot[0])
    CHILDREN.clear(); CHILDREN.update(snapshot[1])


@pytest.fixture()
def client(app_for, ms, monkeypatch):  # noqa: F811
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: TEXT[pdf])
    return app_for(ms)


def connect_mailbox(client, ms):
    """Press Connect mailbox and come back from Microsoft. -> the sign-in address's parameters."""
    resp = client.post("/mail/connect", headers=ORIGIN)
    assert resp.status_code == 200
    href = html.unescape(re.search(r'<a id="continue-sign-in" href="([^"]+)"', resp.data.decode()).group(1))
    q = {k: v[0] for k, v in parse_qs(urlparse(href).query).items()}
    ms.challenge = q["code_challenge"]
    resp = client.get(f"/microsoft/callback?code=AUTH-CODE&state={q['state']}")
    assert resp.status_code == 303 and "msg=mail_connected" in resp.headers["Location"]
    return q


def choose_mail_folder(client, folder_id="CARMAIL", car_id=1):
    return client.post(f"/mail/folders/{car_id}", data={"folder_id": folder_id}, headers=ORIGIN)


@pytest.fixture()
def mailbox(client, ms):
    save_credentials(client)
    connect_mailbox(client, ms)
    assert choose_mail_folder(client).status_code == 303
    return client


def scan(client):
    resp = client.get("/mail/import/1/scan")
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def read_row(client, n, f):
    resp = client.post("/mail/import/1/read", headers=ORIGIN, data={"n": n, **{k: f[k] for k in ("id", "name", "folder", "url")}})
    assert resp.status_code == 200
    return resp.data.decode()


def cat(admin, name):
    return str(admin.execute("SELECT id FROM garagebooks.category WHERE name = %s", (name,)).fetchone()["id"])


# ---- sign-in ---------------------------------------------------------------------

def test_mailbox_signs_in_with_the_same_registration_asking_only_to_read_mail(client, ms, admin):
    save_credentials(client)
    q = connect_mailbox(client, ms)
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://localhost/microsoft/callback"     # no second redirect URI to register
    assert set(q["scope"].split()) == {"Mail.Read", "User.Read", "offline_access"}
    row = account(admin)
    assert row["mail_refresh_token"] and "RT-1" not in row["mail_refresh_token"]     # encrypted
    assert row["mail_account"] == "Test Owner"
    assert row["refresh_token"] is None, "connecting the mailbox doesn't connect OneDrive"


def test_mailbox_and_onedrive_keep_separate_sign_ins(client, ms, admin):
    sign_in(client, ms)
    drive_token = account(admin)["refresh_token"]
    connect_mailbox(client, ms)
    row = account(admin)
    assert row["refresh_token"] == drive_token and row["mail_refresh_token"]
    resp = client.post("/mail/disconnect", headers=ORIGIN)
    assert resp.status_code == 303
    row = account(admin)
    assert row["mail_refresh_token"] is None and row["refresh_token"] == drive_token


def test_storage_page_offers_the_mailbox_once_registered(client, ms):
    page = client.get("/storage").data.decode()
    assert "Connect mailbox</button>" not in page            # no registration yet
    save_credentials(client)
    page = client.get("/storage").data.decode()
    assert "Connect mailbox</button>" in page and "Mail.Read" in page


def test_refresh_for_mail_asks_for_the_mail_scope(mailbox, ms, monkeypatch):
    an_hour_passes(monkeypatch)
    ms.scopes.clear()
    scan(mailbox)
    assert ms.scopes and all("Mail.Read" in s and "Files" not in s for s in ms.scopes)


# ---- a folder per car ------------------------------------------------------------

def test_browse_mail_folders_and_choose_one(client, ms, admin):
    save_credentials(client)
    connect_mailbox(client, ms)
    page = client.get("/mail/browse/1").data.decode()
    assert "Inbox" in page and "Create folder" not in page     # Mail.Read can't make folders
    page = client.get("/mail/browse/1?folder=INBOX").data.decode()
    assert "Test car invoices" in page
    page = client.get("/mail/browse/1?folder=CARMAIL").data.decode()
    assert "/Inbox/Test car invoices" in page
    assert choose_mail_folder(client).status_code == 303
    row = admin.execute("SELECT * FROM garagebooks.car_mail_folder").fetchone()
    assert row["folder_id"] == "CARMAIL" and row["folder_path"] == "/Inbox/Test car invoices"
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.car_folder").fetchone()["n"] == 0, \
        "a mailbox folder is where invoices come from, not where they are kept"
    assert "/Inbox/Test car invoices" in client.get("/storage").data.decode()


def test_the_top_of_the_mailbox_cant_be_chosen(client, ms, admin):
    save_credentials(client)
    connect_mailbox(client, ms)
    assert choose_mail_folder(client, folder_id="").status_code == 400
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.car_mail_folder").fetchone()["n"] == 0


def test_mail_folder_can_be_cleared(mailbox, admin):
    assert mailbox.post("/mail/folders/1/clear", headers=ORIGIN).status_code == 303
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.car_mail_folder").fetchone()["n"] == 0


def test_mail_routes_need_the_encryption_key(app_for, ms, admin):
    c = app_for(ms, key=None)
    assert c.get("/mail/browse/1").status_code == 404
    assert c.post("/mail/connect", headers=ORIGIN).status_code == 404


# ---- importing -------------------------------------------------------------------

def test_scan_lists_pdf_attachments_and_emails_without_one(mailbox):
    files = scan(mailbox)["files"]
    names = [f["name"] for f in files]
    assert names == ["INV12345.pdf", "Refund processed for order INV12345", "Hello"]
    assert "logo.png" not in names
    assert all(f["url"].startswith("https://outlook.live.com/") for f in files)
    assert len({f["url"] for f in files}) == 3
    assert "Example Tyres" in files[0]["folder"]          # who sent it, shown under the name


def test_attachment_row_reads_the_pdf(mailbox, admin):
    f = scan(mailbox)["files"][0]
    row = read_row(mailbox, 0, f)
    assert 'value="2026-10-24"' in row and 'value="1234.50"' in row
    assert re.search(rf'<option value="{cat(admin, "Tyres")}"[^>]*selected>', row)
    assert 'name="include_0" checked' in row
    assert 'name="rename_0"' not in row, "an email's attachment isn't renamed in place"


def test_refund_email_without_attachment_is_a_negative_amount(mailbox, admin):
    f = scan(mailbox)["files"][1]
    row = read_row(mailbox, 1, f)
    assert 'value="-45.00"' in row
    assert 'value="2026-10-30"' in row           # the day it was received (Brisbane time)
    assert 'value="Example Tyres Pty Ltd"' in row     # the company the text names, as for a PDF


def test_email_vendor_falls_back_to_the_sender(mailbox, ms):
    ms.messages = [message("M9", "Refund", "Example Tyres", "2026-10-30T02:00:00Z", body="We have refunded $5.00.")]
    f = scan(mailbox)["files"][0]
    assert 'value="Example Tyres"' in read_row(mailbox, 0, f)


def test_email_with_nothing_to_read_starts_unticked(mailbox):
    f = scan(mailbox)["files"][2]
    row = read_row(mailbox, 2, f)
    assert 'name="include_2" checked' not in row and "read the category, amount" in row


def confirm(client, admin, files, **over):
    data = {"count": str(len(files))}
    for n, (f, values) in enumerate(files):
        data.update({f"id_{n}": f["id"], f"url_{n}": f["url"], f"name_{n}": f["name"], f"folder_{n}": f["folder"],
                     f"include_{n}": "on", **{f"{k}_{n}": v for k, v in values.items()}})
    data.update(over)
    return client.post("/mail/import/1", headers=ORIGIN, data=data)


def test_import_without_storage_links_the_email(mailbox, admin):
    files = scan(mailbox)["files"]
    resp = confirm(mailbox, admin, [(files[1], {"date": "2026-10-30", "category_id": cat(admin, "Tyres"),
                                                "vendor": "Example Tyres", "amount": "-45.00"})])
    assert resp.status_code == 303, resp.data
    (e,) = rows(admin)
    assert e["amount"] == -45 and e["invoice_url"] == files[1]["url"]
    assert [f["name"] for f in scan(mailbox)["files"]] == ["INV12345.pdf", "Hello"], "imported once only"


def test_import_copies_the_attachment_into_the_cars_storage(mailbox, ms, admin):
    q = start_sign_in(mailbox, ms)                      # OneDrive too, on the registration already saved
    assert mailbox.get(f"/microsoft/callback?code=AUTH-CODE&state={q['state']}").status_code == 303
    choose_folder(mailbox)                              # OneDrive folder for the car's invoices
    f = scan(mailbox)["files"][0]
    resp = confirm(mailbox, admin, [(f, {"date": "2026-10-24", "category_id": cat(admin, "Tyres"),
                                         "vendor": "Example Tyres", "amount": "1234.50"})])
    assert resp.status_code == 303, resp.data
    (upload,) = ms.uploads()
    assert upload[2] == TYRES_PDF and "Tyres" in unquote(upload[1])
    (e,) = rows(admin)
    assert e["invoice_url"] == "https://onedrive.live.com/?id=FILE1"
    assert scan(mailbox)["files"][0]["name"] != "INV12345.pdf", "the email it came from isn't offered again"


def test_deleting_the_expense_offers_the_email_again(mailbox, admin):
    files = scan(mailbox)["files"]
    confirm(mailbox, admin, [(files[1], {"date": "2026-10-30", "category_id": cat(admin, "Tyres"),
                                         "vendor": "Example Tyres", "amount": "-45.00"})])
    (e,) = rows(admin)
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (e["id"],))
    assert len(scan(mailbox)["files"]) == 3


def test_skipped_email_isnt_offered_again(mailbox, admin):
    files = scan(mailbox)["files"]
    resp = confirm(mailbox, admin, [(files[2], {})], include_0="", skip_0="on")
    assert resp.status_code == 303
    assert [f["name"] for f in scan(mailbox)["files"]] == ["INV12345.pdf", "Refund processed for order INV12345"]


def test_import_page_opens_from_the_storage_page(mailbox):
    page = mailbox.get("/storage").data.decode()
    assert "/mail/import/1" in page
    page = mailbox.get("/mail/import/1").data.decode()
    assert "mailbox folder but not yet in your expenses" in page


def test_mail_import_needs_a_folder(client, ms):
    save_credentials(client)
    connect_mailbox(client, ms)
    assert client.get("/mail/import/1/scan").status_code == 409


# ---- watching the folder ---------------------------------------------------------

def test_mail_job_counts_whats_waiting_and_a_notice_says_so(mailbox, app_for, ms, admin):
    app = mailbox.application
    assert "mail" in app.extensions["scheduler"].jobs
    with app.app_context():
        message = app.extensions["scheduler"].jobs["mail"].run(admin)
    assert "3" in message
    row = admin.execute("SELECT * FROM garagebooks.car_mail_folder").fetchone()
    assert row["waiting"] == 3 and row["newest_at"] == datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    found = notices.mail_notices(admin)
    assert len(found) == 1 and "3 emails" in found[0].title and "Test car" in found[0].title
    first_key = found[0].key
    ms.messages.append(message_("M4", "Another invoice", "2026-11-02T03:00:00Z"))
    with app.app_context():
        app.extensions["scheduler"].jobs["mail"].run(admin)
    (again,) = notices.mail_notices(admin)
    assert again.key != first_key, "a new email is a new notice"


def message_(mid, subject, received):
    return message(mid, subject, "Example Tyres", received, body="Total  $10.00\n")


def test_mail_job_is_quiet_when_no_mailbox_is_connected(client, admin):
    app = client.application
    with app.app_context():
        assert app.extensions["scheduler"].jobs["mail"].run(admin) == "no mailbox connected"


def test_lost_mail_sign_in_is_noted_and_told(mailbox, ms, admin, monkeypatch):
    an_hour_passes(monkeypatch)
    ms.refresh_ok = False
    app = mailbox.application
    with app.app_context():
        jobs = app.extensions["scheduler"].jobs
        try:
            jobs["mail"].run(admin)
        except Exception:   # noqa: BLE001 - the job reports the failure; what matters is what was noted
            pass
    row = account(admin)
    assert row["mail_signin_lost_at"] is not None
    (n,) = notices.mail_signin_notices(row, datetime.now(timezone.utc))
    assert "mailbox" in n.title.lower()


# ---- reading refunds ---------------------------------------------------------------

@pytest.mark.parametrize("subject,text,expected", [
    ("Refund processed", "We have refunded $45.00 to your card.", True),
    ("Your receipt", "Your refund of $12.50 has been processed.", True),
    ("Credit note CN1001", "Credit note\nTotal  $20.00", True),
    ("Your invoice", "Invoice\nTotal  $120.00\nSee our refund policy at example.com.", False),
    ("Booking confirmed", "See you on Tuesday.", False),
])
def test_is_refund(subject, text, expected):
    assert invoices.is_refund(subject, text) is expected


@pytest.mark.parametrize("text,amount", [
    ("We have refunded $45.00 to your card.", "45.00"),
    ("Refund amount:  $1,200.00", "1200.00"),
    ("Your refund of $12.50 has been processed.", "12.50"),
    ("Total refunded  $8.00", "8.00"),
    # The figure on its own line, with the word under it (an insurer's refund email, Windows line ends).
    ("Refund for your policy has been sent\r\n\r\nReceipt number:12345678\r\n\r\n$1,234.50\r\nRefunded 12 Feb 2026\r\n",
     "1234.50"),
    ("Your refund\n\n$20.00\n\nRefund amount\n", "20.00"),
])
def test_refund_amount(text, amount):
    from decimal import Decimal
    assert invoices.refund_amount(text) == Decimal(amount)


def test_mail_ids_never_reach_the_log_with_tokens(mailbox, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    f = scan(mailbox)["files"][0]
    read_row(mailbox, 0, f)
    text = caplog.text
    assert "AT-" not in text and "RT-" not in text and base64.b64encode(b"RT-1").decode() not in text


def test_app_role_can_use_mail_tables(pg, admin):
    import psycopg
    with psycopg.connect(pg.app_url, autocommit=True) as conn:
        conn.execute("SELECT * FROM garagebooks.car_mail_folder")
        conn.execute("SELECT * FROM garagebooks.mail_import")
        conn.execute("SELECT mail_refresh_token, mail_account, mail_signin_lost_at FROM garagebooks.onedrive_account")


def test_received_date_is_local(mailbox, admin, ms):
    """An email received at 23:30 UTC on the 30th is the 31st in Brisbane."""
    ms.messages = [message("M9", "Refund", "Example Tyres", "2026-10-30T23:30:00Z", body="We have refunded $5.00.")]
    f = scan(mailbox)["files"][0]
    assert 'value="2026-10-31"' in read_row(mailbox, 0, f)



# ---- the Microsoft account and Outlook marks -------------------------------------

def test_the_microsoft_account_and_mailbox_cards_carry_their_marks(client):
    save_credentials(client)
    page = client.get("/storage").data.decode()
    microsoft = page.split('id="microsoft"', 1)[1].split("</section>", 1)[0]
    mail = page.split('id="mail"', 1)[1].split("</section>", 1)[0]
    assert re.search(r'<h2><img src="[^"]*/provider-icon/microsoft" alt="" class="provider-icon"> Microsoft account', microsoft)
    assert re.search(r'<h2><img src="[^"]*/provider-icon/outlook" alt="" class="provider-icon"> Invoices by email', mail)
    assert re.search(r'<img src="[^"]*/provider-icon/outlook"[^>]*>\s*Connect mailbox</button>', mail)
    assert "trademarks of Microsoft" in microsoft


def test_the_marks_are_served_with_a_bundled_stand_in(client):
    for name in ("microsoft", "outlook"):
        resp = client.get(f"/provider-icon/{name}")
        assert resp.status_code == 200 and resp.mimetype == "image/svg+xml" and b"<svg" in resp.data


def test_the_marks_need_the_onedrive_set_up(app_for, ms, admin):
    c = app_for(ms, key=None)
    assert c.get("/provider-icon/microsoft").status_code == 404


# ---- opening the email and its PDF from the import page ---------------------------

def test_attachment_row_opens_both_the_pdf_and_its_email(mailbox):
    f = scan(mailbox)["files"][0]
    row = read_row(mailbox, 0, f)
    assert "/mail/file/1?id=M1+A1" in row or "/mail/file/1?id=M1%20A1" in row
    assert f'href="{WEBLINK.format("M1")}"' in row.replace("&amp;", "&"), "the email itself, without the attachment part"
    resp = mailbox.get("/mail/file/1", query_string={"id": "M1 A1"})
    assert resp.status_code == 200 and resp.mimetype == "application/pdf" and resp.data == TYRES_PDF
    assert "inline" in resp.headers["Content-Disposition"] and "INV12345.pdf" in resp.headers["Content-Disposition"]


def test_email_row_opens_the_email_only(mailbox):
    f = scan(mailbox)["files"][1]
    row = read_row(mailbox, 1, f)
    assert "/mail/file/" not in row and f'href="{WEBLINK.format("M2")}"' in row.replace("&amp;", "&")


def test_mail_file_serves_only_pdf_attachments(mailbox, ms):
    assert mailbox.get("/mail/file/1", query_string={"id": "M2"}).status_code == 404       # an email, no attachment
    ms.messages[0]["attachments"][0]["_content"] = b"<html>not a pdf</html>"
    assert mailbox.get("/mail/file/1", query_string={"id": "M1 A1"}).status_code == 404
    assert mailbox.get("/mail/file/2", query_string={"id": "M1 A1"}).status_code == 404     # no such car
