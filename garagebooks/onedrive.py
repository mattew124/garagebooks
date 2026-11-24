"""Uploading invoice PDFs to a personal OneDrive through Microsoft Graph.

Sign-in is the standard web flow (OAuth 2.0 authorisation code with PKCE):
Connect sends the browser to Microsoft, which sends it back to /microsoft/callback (/onedrive/callback for a registration saved before it)
with a code the app swaps for tokens. Microsoft only redirects to addresses
registered on the app, and every installation has its own address, so each
installer registers their own Entra app and enters its client ID and secret on
the Invoice storage page. Personal Microsoft accounts can't use app-only (client
credentials) access to OneDrive, so a delegated sign-in is the only way in.

The refresh token is the key to the whole drive (Files.ReadWrite), so it and the
client secret are encrypted with GARAGEBOOKS_ENCRYPTION_KEY before they reach the
database: a backup of the TeslaMate database on its own can't open the drive.

Every HTTP call goes through `transport(method, url, data, headers) -> (status, json)`,
which tests replace with a fake. Downloading a file is the one exception: the transport
is called with raw=True and returns (status, bytes).
"""

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken

from .storage import StorageError

log = logging.getLogger("garagebooks.onedrive")

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Files.ReadWrite offline_access"
# The mailbox (outlook.py) is a second sign-in on the same app registration, asking only to read mail
# (and the account's name, to show who is connected). It never shares a token with the drive.
MAIL_SCOPES = "Mail.Read User.Read offline_access"
BUSY_STATUSES = (429, 503)   # Microsoft throttling or a brief outage: worth a short wait
RETRY_WAITS = (1, 3)         # seconds between tries; requests are bounded by gunicorn's timeout
MAX_PAGES = 20   # of 200 children each, when listing a folder
MAX_DELTA_PAGES = 100   # of 200 items each, when listing a folder tree with delta
MAX_DEPTH = 4    # subfolders below a car's folder that are searched for PDFs
MAX_PDF_BYTES = 20 * 1024 * 1024


class OneDriveError(StorageError):
    """Carries a message fit to show the user."""


class SignInExpired(OneDriveError):
    pass


class NotFound(OneDriveError):
    """Graph answered 404: the item isn't there (moved to another drive, or deleted)."""


class TooLarge(OneDriveError):
    """A file over the size the caller asked for: refused before its bytes are downloaded."""


class NotConnected(OneDriveError):
    def __init__(self, user_message="Connect OneDrive first (Invoice storage page), then try again."):
        super().__init__(user_message)

    @classmethod
    def mailbox(cls):
        return cls("Connect the mailbox first (Invoice storage page), then try again.")


def http(method, url, data=None, headers=None, timeout=60, raw=False):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OneDriveError(f"Couldn't reach Microsoft: {exc}") from None
    if raw:
        return status, body
    try:
        return status, json.loads(body or b"{}")
    except ValueError:
        return status, {}


def token_box(key):
    """GARAGEBOOKS_ENCRYPTION_KEY -> Fernet. Raises ValueError naming the variable."""
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "GARAGEBOOKS_ENCRYPTION_KEY must be 32 random bytes, url-safe base64 encoded "
            "(see https://mattew124.github.io/garagebooks/onedrive/)") from None


def graph_error(status, body):
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return f"{err.get('message') or err.get('code') or 'error'} (HTTP {status})"
    return f"HTTP {status}"


def new_pkce():
    """-> (state, verifier, challenge) for one sign-in."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.token_urlsafe(32), verifier, challenge


def display_path(item):
    """Graph item -> '/Cars/Model 3' (the drive root is '/')."""
    parent = (item.get("parentReference") or {}).get("path")
    if not parent:
        return "/"
    _, _, under_root = parent.partition("root:")
    return f"{under_root.rstrip('/')}/{item['name']}"


DRIVE_HOSTS = ("onedrive.live.com", "1drv.ms")


def is_drive_url(web_url):
    """Is this an address of a file in a personal OneDrive (what an upload or a OneDrive import keeps)?"""
    try:
        u = urlparse(str(web_url or ""))
    except ValueError:
        return False
    return u.scheme == "https" and u.hostname in DRIVE_HOSTS


def item_id_from_url(web_url):
    """A personal OneDrive file's webUrl (https://onedrive.live.com?cid=...&id=<item id>, or the older
    redir?resid=<item id>) -> the item id, or None when the address doesn't hold one (a 1drv.ms link)."""
    u = urlparse(web_url)
    if u.scheme != "https" or u.hostname != "onedrive.live.com":
        return None
    query = parse_qs(u.query)
    found = (query.get("id") or query.get("resid") or [""])[0].strip()
    return found or None


def share_token(web_url):
    """Graph's id for an item by its address (GET /shares/{token}/driveItem): u! and the address in unpadded
    url-safe base64."""
    return "u!" + base64.urlsafe_b64encode(web_url.encode()).rstrip(b"=").decode()


def folder_info(item):
    """Graph folder item -> {id, name, path, parent_id, is_root}."""
    parent_id = (item.get("parentReference") or {}).get("id")
    return {"id": item["id"], "name": item["name"], "path": display_path(item),
            "parent_id": parent_id, "is_root": "root" in item or parent_id is None}


class OneDrive:
    def __init__(self, key, transport=None):
        self.box = token_box(key)
        self.transport = transport or http

    def encrypt(self, text):
        return self.box.encrypt(text.encode()).decode()

    def decrypt(self, token):
        try:
            return self.box.decrypt(token.encode()).decode()
        except InvalidToken:
            raise SignInExpired(
                "The saved OneDrive details can't be read (was GARAGEBOOKS_ENCRYPTION_KEY changed?). "
                "Enter the client secret and connect again on the Invoice storage page.") from None

    # ---- sign-in --------------------------------------------------------------

    @staticmethod
    def authorize_url(client_id, redirect_uri, state, challenge, scopes=SCOPES):
        return f"{AUTHORITY}/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "response_mode": "query", "scope": scopes, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "select_account",
        })

    def _token(self, client_id, secret, fields):
        data = urlencode({"client_id": client_id, "client_secret": secret, **fields}).encode()
        return self.transport("POST", f"{AUTHORITY}/token", data,
                              {"Content-Type": "application/x-www-form-urlencoded"})

    def redeem_code(self, client_id, secret, code, redirect_uri, verifier, scopes=SCOPES):
        status, body = self._token(client_id, secret, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "scope": scopes,
        })
        if status == 200 and "refresh_token" in body:
            return body
        raise OneDriveError("Microsoft didn't complete the sign-in: "
                            + (body.get("error_description") or body.get("error") or f"HTTP {status}"))

    def refresh(self, client_id, secret, refresh_token, scopes=SCOPES, what="OneDrive"):
        """-> {access_token, refresh_token}. Microsoft rotates the refresh token:
        the caller must store the new one. `what` names the sign-in in messages (OneDrive, the mailbox)."""
        status, body = self._token(client_id, secret, {
            "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scopes,
        })
        if status == 200 and "access_token" in body:
            return body
        if body.get("error") in ("invalid_grant", "interaction_required"):
            raise SignInExpired(f"The {what} sign-in has expired: sign in again on the Invoice storage page.")
        if body.get("error") in ("invalid_client", "unauthorized_client"):
            raise SignInExpired("Microsoft refused the client secret (expired?): enter a new one "
                                "on the Invoice storage page.")
        raise OneDriveError(f"Couldn't refresh the {what} sign-in: "
                            + (body.get("error_description") or f"HTTP {status}"))

    # ---- Graph ----------------------------------------------------------------

    def _get(self, access_token, url, prefer=None, service="OneDrive"):
        """GET from Graph, waiting a little if it is busy. `prefer` is a Prefer header (outlook.py
        asks for immutable ids and plain-text bodies); `service` names it in messages."""
        headers = {"Authorization": f"Bearer {access_token}"}
        if prefer:
            headers["Prefer"] = prefer
        for wait in (*RETRY_WAITS, None):
            status, body = self.transport("GET", url, None, headers)
            if status not in BUSY_STATUSES or wait is None:
                break
            log.warning("%s busy (HTTP %d): trying again in %ds", service, status, wait)
            time.sleep(wait)
        if status in BUSY_STATUSES:
            raise OneDriveError(f"{service} is busy right now: try again in a few minutes.")
        if status != 200:
            raise (NotFound if status == 404 else OneDriveError)(f"{service} said no: " + graph_error(status, body))
        return body

    def account_name(self, access_token):
        try:
            user = (self._get(access_token, f"{GRAPH}/me/drive").get("owner") or {}).get("user") or {}
        except OneDriveError:
            return None
        return user.get("displayName") or user.get("email")

    def folder(self, access_token, folder_id=None):
        """-> {id, name, path, parent_id} of a folder (the drive root when None)."""
        url = f"{GRAPH}/me/drive/items/{quote(folder_id, safe='')}" if folder_id else f"{GRAPH}/me/drive/root"
        item = self._get(access_token, url)
        if "folder" not in item:
            raise OneDriveError("That isn't a folder.")
        return folder_info(item)

    def subfolders(self, access_token, folder_id=None):
        """Folders directly inside a folder, by name: [{id, name}]."""
        base = f"{GRAPH}/me/drive/items/{quote(folder_id, safe='')}" if folder_id else f"{GRAPH}/me/drive/root"
        url = f"{base}/children?$top=200&$select=id,name,folder"
        found = []
        for _ in range(MAX_PAGES):
            body = self._get(access_token, url)
            found += [{"id": c["id"], "name": c["name"]} for c in body.get("value", []) if "folder" in c]
            url = body.get("@odata.nextLink")
            if not url or not url.startswith(GRAPH + "/"):
                break
        return sorted(found, key=lambda f: f["name"].lower())

    def contents(self, access_token, folder_id=None):
        """What a file picker shows for one folder, in a single Graph call: the folder itself and
        its folders and PDFs, by name. -> (folder, [{id, name}], [{id, name, size, web_url}]),
        the folder as `folder()` gives it. Graph answers the first 200 children with the folder;
        any more come in pages."""
        base = f"{GRAPH}/me/drive/items/{quote(folder_id, safe='')}" if folder_id else f"{GRAPH}/me/drive/root"
        item = self._get(access_token, f"{base}?$expand=children($select=id,name,size,folder,file,webUrl)")
        if "folder" not in item:
            raise OneDriveError("That isn't a folder.")
        children = item.get("children", [])
        url = item.get("children@odata.nextLink")
        for _ in range(MAX_PAGES - 1):
            if not url or not url.startswith(GRAPH + "/"):
                break
            body = self._get(access_token, url)
            children += body.get("value", [])
            url = body.get("@odata.nextLink")
        folders, files = [], []
        for c in children:
            if "folder" in c:
                folders.append({"id": c["id"], "name": c["name"]})
            elif "file" in c and c["name"].lower().endswith(".pdf") and str(c.get("webUrl", "")).startswith("https://"):
                files.append({"id": c["id"], "name": c["name"], "size": c.get("size", 0), "web_url": c["webUrl"]})
        by_name = lambda x: x["name"].lower()   # noqa: E731
        return folder_info(item), sorted(folders, key=by_name), sorted(files, key=by_name)

    def create_folder(self, access_token, parent_id, name):
        """New folder inside parent_id (the drive root when None). Fails rather
        than renaming if the name is taken. -> {id, name, path, parent_id}"""
        base = f"{GRAPH}/me/drive/items/{quote(parent_id, safe='')}" if parent_id else f"{GRAPH}/me/drive/root"
        body = json.dumps({"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}).encode()
        status, item = self.transport("POST", f"{base}/children", body, {
            "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
        if status == 409:
            raise OneDriveError(f"A folder called “{name}” already exists here.")
        if status not in (200, 201) or "id" not in item:
            raise OneDriveError("OneDrive didn't create the folder: " + graph_error(status, item))
        return {"id": item["id"], "name": item["name"], "path": display_path(item),
                "parent_id": (item.get("parentReference") or {}).get("id")}

    def rename(self, access_token, item_id, new_name):
        """Rename a file in place; fails rather than replacing a file with that name.
        -> its web URL (which can change with the name)."""
        body = json.dumps({"name": new_name, "@microsoft.graph.conflictBehavior": "fail"}).encode()
        status, item = self.transport("PATCH", f"{GRAPH}/me/drive/items/{quote(item_id, safe='')}", body, {
            "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
        if status == 409:
            log.warning("rename item=%s to=%r: name already exists", item_id, new_name)
            raise OneDriveError(f"A file called “{new_name}” already exists there.")
        if status != 200 or not str(item.get("webUrl", "")).startswith("https://"):
            log.warning("rename item=%s to=%r: %s", item_id, new_name, graph_error(status, item))
            raise OneDriveError("OneDrive didn't rename the file: " + graph_error(status, item))
        log.debug("rename item=%s to=%r", item_id, new_name)
        return item["webUrl"]

    def ensure_subfolder(self, access_token, parent_id, name):
        """The folder called `name` (any case) directly inside parent_id, made if it
        isn't there. -> its id. Two uploads racing to make it both end up with one."""
        def find():
            return next((f["id"] for f in self.subfolders(access_token, parent_id)
                         if f["name"].lower() == name.lower()), None)
        found = find()
        if found:
            return found
        try:
            made = self.create_folder(access_token, parent_id, name)["id"]
            log.debug("subfolder created name=%r parent=%s", name, parent_id)
            return made
        except OneDriveError:
            found = find()   # lost the race: it exists now
            if found:
                return found
            raise

    def upload(self, access_token, folder_id, filename, content):
        """Simple upload (fine to 250 MB) into a folder, by id. A name that
        already exists gets a suffix rather than being overwritten.
        -> the file's OneDrive web URL."""
        url = (f"{GRAPH}/me/drive/items/{quote(folder_id, safe='')}:/{quote(filename, safe='')}:/content"
               "?@microsoft.graph.conflictBehavior=rename")
        status, body = self.transport("PUT", url, content, {
            "Authorization": f"Bearer {access_token}", "Content-Type": "application/pdf",
        })
        if status not in (200, 201) or not str(body.get("webUrl", "")).startswith("https://"):
            log.warning("upload folder=%s name=%r refused: %s", folder_id, filename, graph_error(status, body))
            raise OneDriveError("OneDrive refused the upload: " + graph_error(status, body))
        return body["webUrl"]

    def pdfs(self, access_token, folder_id):
        """Every PDF in a folder and the folders below it (MAX_DEPTH deep), oldest
        name first: [{id, name, size, web_url, folder}]. `folder` is the path below
        the one given, '' for files directly in it.

        One `driveItem: delta` query lists the whole subtree in pages of 200 items, where listing
        `/children` costs a call for every folder as well as every page. If Graph won't answer a delta
        query for the folder, the folders are walked with `/children` as before."""
        try:
            found = self._pdfs_by_delta(access_token, folder_id)
        except OneDriveError as exc:
            log.warning("delta listing of folder=%s refused (%s): listing it folder by folder", folder_id, exc)
            found = self._pdfs_by_children(access_token, folder_id)
        log.debug("list folder=%s pdfs=%d", folder_id, len(found))
        return sorted(found, key=lambda f: (f["folder"].lower(), f["name"].lower()))

    @staticmethod
    def _is_listed_pdf(c):
        return "file" in c and c["name"].lower().endswith(".pdf") and str(c.get("webUrl", "")).startswith("https://")

    def _pdfs_by_delta(self, access_token, folder_id):
        """The PDFs of pdfs(), from a delta query for the folder. Delta gives each item only its parent's id
        (no path), in any order, so a file's folder is worked out by following the parents up to `folder_id`."""
        url = (f"{GRAPH}/me/drive/items/{quote(folder_id, safe='')}/delta"
               f"?$select=id,name,size,folder,file,webUrl,parentReference&$top=200")
        items = []
        for _ in range(MAX_DELTA_PAGES):
            body = self._get(access_token, url)
            items += [c for c in body.get("value", []) if "deleted" not in c and c.get("id")]
            url = body.get("@odata.nextLink")
            if not url or not url.startswith(GRAPH + "/"):
                break
        folders = {c["id"]: c for c in items if "folder" in c}
        paths = {}

        def below(fid):
            """Names from `folder_id` down to the folder `fid`, or None when it isn't under it (or too deep)."""
            if fid == folder_id:
                return []
            if fid not in paths:
                paths[fid] = None           # also stops a loop of parents
                here = folders.get(fid)
                parent = below((here.get("parentReference") or {}).get("id")) if here else None
                if parent is not None and len(parent) < MAX_DEPTH:
                    paths[fid] = [*parent, here["name"]]
            return paths[fid]

        found = []
        for c in items:
            if not self._is_listed_pdf(c):
                continue
            names = below((c.get("parentReference") or {}).get("id"))
            if names is not None:
                found.append({"id": c["id"], "name": c["name"], "size": c.get("size", 0),
                              "web_url": c["webUrl"], "folder": "/".join(names)})
        return found

    def _pdfs_by_children(self, access_token, folder_id):
        found = []

        def walk(fid, below, depth):
            url = (f"{GRAPH}/me/drive/items/{quote(fid, safe='')}/children"
                   "?$top=200&$select=id,name,size,folder,file,webUrl")
            for _ in range(MAX_PAGES):
                body = self._get(access_token, url)
                for c in body.get("value", []):
                    if "folder" in c and depth < MAX_DEPTH:
                        walk(c["id"], f"{below}/{c['name']}" if below else c["name"], depth + 1)
                    elif self._is_listed_pdf(c):
                        found.append({"id": c["id"], "name": c["name"], "size": c.get("size", 0),
                                      "web_url": c["webUrl"], "folder": below})
                url = body.get("@odata.nextLink")
                if not url or not url.startswith(GRAPH + "/"):
                    break

        walk(folder_id, "", 0)
        return found

    def download(self, access_token, item_id):
        """A file's bytes. Graph hands out a short-lived pre-authenticated address on
        the CDN, which must be fetched without the bearer token."""
        meta = self._get(access_token, f"{GRAPH}/me/drive/items/{quote(item_id, safe='')}"
                                       "?select=id,size,@microsoft.graph.downloadUrl")
        link = meta.get("@microsoft.graph.downloadUrl", "")
        if not link.startswith("https://") or meta.get("size", 0) > MAX_PDF_BYTES:
            raise OneDriveError("That file can't be downloaded (missing or over 20 MB).")
        return self._from_cdn(link, item_id)

    def _from_cdn(self, link, item_id):
        status, content = self.transport("GET", link, None, {}, raw=True)
        if status != 200:
            log.warning("download item=%s refused: HTTP %s", item_id, status)
            raise OneDriveError(f"OneDrive didn't give the file (HTTP {status}).")
        log.debug("download item=%s bytes=%d", item_id, len(content))
        return content

    def fetch(self, access_token, web_url, max_bytes=MAX_PDF_BYTES):
        """A file in this drive, by the address an expense keeps for it (its invoice_url, the file's webUrl)
        -> (its name, its bytes). The item id is read from the address when it holds one (as a personal
        drive's do), otherwise Graph's shares lookup finds the item. Raises NotFound when the file has gone,
        and TooLarge, before downloading anything, when it is over max_bytes."""
        item_id = item_id_from_url(web_url)
        where = (f"{GRAPH}/me/drive/items/{quote(item_id, safe='')}" if item_id
                 else f"{GRAPH}/shares/{share_token(web_url)}/driveItem")
        try:
            meta = self._get(access_token, f"{where}?select=id,name,size,file,@microsoft.graph.downloadUrl")
        except NotFound:
            log.warning("fetch by address: the file isn't in OneDrive (by %s)", "item id" if item_id else "share")
            raise NotFound("The invoice file isn't in OneDrive any more (moved or deleted?).") from None
        link = str(meta.get("@microsoft.graph.downloadUrl", ""))
        if "file" not in meta or not link.startswith("https://"):
            raise OneDriveError("That OneDrive address isn't a file that can be downloaded.")
        if meta.get("size", 0) > max_bytes:
            raise TooLarge(f"The file is over {max_bytes // (1024 * 1024)} MB.")
        content = self._from_cdn(link, meta.get("id"))
        if len(content) > max_bytes:
            raise TooLarge(f"The file is over {max_bytes // (1024 * 1024)} MB.")
        return str(meta.get("name") or "invoice.pdf"), content
