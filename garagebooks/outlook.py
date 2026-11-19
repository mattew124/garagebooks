"""Reading invoices from an Outlook.com mailbox through Microsoft Graph.

Each car can have a mailbox folder (a rule in Outlook files the garage's emails there, say). Its emails
are offered on the same import page as a OneDrive or server folder: every PDF attached to an email is
one item, and an email with no PDF is an item of its own, read from its text (a refund notice, which
says what came back but attaches nothing).

The mailbox is signed in to with the installation's OneDrive app registration, as a second sign-in that
only asks to read mail (onedrive.MAIL_SCOPES), so a OneDrive-only installation never grants it and the
mailbox can belong to a different account from the drive. Nothing here writes to the mailbox: an email
is never moved, flagged, marked read or deleted.

An item's id is the email's id, or the email's and the attachment's ids with a space between (Graph ids
never hold one). Ids are asked for as immutable ids, so moving an email between folders keeps them. An
item's `web_url`, what the import page and the expense keep, is the email's own Outlook on the web link,
with `#attachment=<n>` added for an attachment, so two PDFs in one email are two items.

Every call goes through the OneDrive object's transport, so tests replace Microsoft in one place.
"""

import logging
from datetime import datetime
from urllib.parse import quote

from .onedrive import GRAPH, MAX_PDF_BYTES, OneDriveError

log = logging.getLogger("garagebooks.outlook")

SERVICE = "Outlook"
ROOT = "msgfolderroot"         # Graph's name for the top of the mailbox, above Inbox and the rest
IMMUTABLE = 'IdType="ImmutableId"'
PAGE = 50
MAX_MESSAGES = 200             # the newest emails in a folder that are looked at; older ones are left alone
MAX_FOLDER_DEPTH = 8           # folders above a chosen one that are walked to show its path
MAX_FOLDERS = 500              # subfolders listed in one folder
MAX_BODY_CHARS = 100_000       # of an email's text: far more than any receipt
MESSAGE_FIELDS = "id,subject,from,receivedDateTime,hasAttachments,webLink"
ATTACHMENT_FIELDS = "id,name,contentType,size,isInline"


class MailError(OneDriveError):
    """Carries a message fit to show the user."""


def _id(value):
    return quote(value, safe="")


def is_pdf(attachment):
    """A file attached to an email that is a PDF (by name or type). Linked and embedded items aren't files."""
    kind = attachment.get("@odata.type", "#microsoft.graph.fileAttachment")
    if kind != "#microsoft.graph.fileAttachment":
        return False
    name = str(attachment.get("name") or "")
    return name.lower().endswith(".pdf") or str(attachment.get("contentType") or "").lower() == "application/pdf"


def sender(message):
    address = ((message.get("from") or {}).get("emailAddress") or {})
    return (address.get("name") or address.get("address") or "").strip()


def received(message):
    """The email's received time, aware (UTC), or None."""
    try:
        return datetime.fromisoformat(str(message.get("receivedDateTime", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def split_id(item_id):
    """'<message id>' or '<message id> <attachment id>' -> (message id, attachment id or None)."""
    message_id, _, attachment_id = (item_id or "").partition(" ")
    if not message_id:
        raise MailError("That email can't be found.")
    return message_id, attachment_id or None


class Mailbox:
    """The mailbox calls, each taking an access token first, as OneDrive's do. `drive` is the
    onedrive.OneDrive whose transport and Graph helper these use."""

    def __init__(self, drive):
        self.drive = drive

    def _get(self, access_token, url, prefer=IMMUTABLE):
        return self.drive._get(access_token, url, prefer=prefer, service=SERVICE)

    def _pages(self, access_token, url, limit):
        found = []
        while url and len(found) < limit:
            body = self._get(access_token, url)
            found += body.get("value", [])
            url = body.get("@odata.nextLink")
            if url and not url.startswith(GRAPH + "/"):
                break
        return found[:limit]

    def account_name(self, access_token):
        try:
            me = self._get(access_token, f"{GRAPH}/me?$select=displayName,mail,userPrincipalName", prefer=None)
        except OneDriveError:
            return None
        return me.get("displayName") or me.get("mail") or me.get("userPrincipalName")

    # ---- folders --------------------------------------------------------------

    def _root(self, access_token):
        return self._get(access_token, f"{GRAPH}/me/mailFolders/{ROOT}?$select=id,displayName")

    def folder(self, access_token, folder_id=None):
        """-> {id, name, path, parent_id, is_root}, as onedrive.folder_info gives a drive folder. The top of
        the mailbox is '/', and a folder directly under it has no parent_id (going up leads to the top)."""
        root = self._root(access_token)
        if not folder_id or folder_id in (ROOT, root["id"]):
            return {"id": root["id"], "name": "Mailbox", "path": "/", "parent_id": None, "is_root": True}
        names, here, first = [], folder_id, None
        for _ in range(MAX_FOLDER_DEPTH):
            f = self._get(access_token, f"{GRAPH}/me/mailFolders/{_id(here)}?$select=id,displayName,parentFolderId")
            first = first or f
            names.append(f.get("displayName") or "?")
            here = f.get("parentFolderId")
            if not here or here == root["id"]:
                break
        parent = first.get("parentFolderId")
        return {"id": first["id"], "name": first.get("displayName") or "?", "path": "/" + "/".join(reversed(names)),
                "parent_id": None if parent == root["id"] else parent, "is_root": False}

    def subfolders(self, access_token, folder_id=None):
        """Folders directly inside one (the top of the mailbox when None), by name: [{id, name}]."""
        url = (f"{GRAPH}/me/mailFolders/{_id(folder_id or ROOT)}/childFolders"
               f"?$top=100&$select=id,displayName")
        found = [{"id": f["id"], "name": f.get("displayName") or "?"}
                 for f in self._pages(access_token, url, MAX_FOLDERS)]
        return sorted(found, key=lambda f: f["name"].lower())

    # ---- emails ---------------------------------------------------------------

    def messages(self, access_token, folder_id):
        """The newest MAX_MESSAGES emails in a folder (not its subfolders), newest first, with their
        attachments' names and sizes (not their content)."""
        url = (f"{GRAPH}/me/mailFolders/{_id(folder_id)}/messages?$top={PAGE}&$select={MESSAGE_FIELDS}"
               f"&$expand=attachments($select={ATTACHMENT_FIELDS})&$orderby=receivedDateTime%20desc")
        return self._pages(access_token, url, MAX_MESSAGES)

    def pdfs(self, access_token, folder_id):
        """What can be imported from a folder, as the import page lists files: [{id, name, size, web_url,
        folder, received}]. `folder` here says when and who from, to show under the name."""
        found = []
        for m in self.messages(access_token, folder_id):
            link = str(m.get("webLink") or "")
            if not link.startswith("https://") or not m.get("id"):
                continue
            when = received(m)
            about = " · ".join(x for x in (when.strftime("%d %b %Y") if when else "", sender(m),
                                           (m.get("subject") or "").strip()) if x)
            pdfs = [a for a in m.get("attachments") or [] if is_pdf(a) and a.get("id")]
            for a in pdfs:
                found.append({"id": f"{m['id']} {a['id']}", "name": a.get("name") or "attachment.pdf",
                              "size": a.get("size", 0), "web_url": f"{link}#attachment={_id(a['id'])}",
                              "folder": about, "received": when})
            if not pdfs:
                found.append({"id": m["id"], "name": (m.get("subject") or "").strip() or "(no subject)",
                              "size": 0, "web_url": link, "folder": " · ".join(x for x in (
                                  when.strftime("%d %b %Y") if when else "", sender(m)) if x),
                              "received": when})
        log.debug("list mail folder messages=%d items=%d", len({f["id"].split(" ")[0] for f in found}), len(found))
        return found

    def message(self, access_token, message_id):
        """One email with its text: {subject, sender, received, text, web_url}."""
        m = self._get(access_token, f"{GRAPH}/me/messages/{_id(message_id)}?$select={MESSAGE_FIELDS},body",
                      prefer=f'{IMMUTABLE}, outlook.body-content-type="text"')
        body = (m.get("body") or {}).get("content") or ""
        return {"subject": (m.get("subject") or "").strip(), "sender": sender(m), "received": received(m),
                "text": body[:MAX_BODY_CHARS], "web_url": m.get("webLink")}

    def download(self, access_token, item_id):
        """An attachment's bytes."""
        message_id, attachment_id = split_id(item_id)
        if attachment_id is None:
            raise MailError("That email has no PDF attached.")
        url = f"{GRAPH}/me/messages/{_id(message_id)}/attachments/{_id(attachment_id)}/$value"
        status, content = self.drive.transport("GET", url, None, {"Authorization": f"Bearer {access_token}",
                                                                  "Prefer": IMMUTABLE}, raw=True)
        if status != 200:
            log.warning("mail attachment download refused: HTTP %s", status)
            raise MailError(f"Outlook didn't give the attachment (HTTP {status}).")
        if len(content) > MAX_PDF_BYTES:
            raise MailError("That attachment is over 20 MB.")
        log.debug("download mail attachment bytes=%d", len(content))
        return content
