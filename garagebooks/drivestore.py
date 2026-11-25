"""The folders and PDFs of the signed-in OneDrive, kept in the database so that browsing waits for nothing.

One Graph `delta` query from the drive root lists everything, a page at a time. That takes minutes on a drive with
tens of thousands of files (delta has no way to ask for folders only), so it runs in the background: each page is
saved with the address of the next, and a listing cut short carries on from there. Its last page gives a delta link,
which from then on answers with only what has changed. Both web workers read the same rows.

Until the first listing is complete, browse() answers None and the caller asks Graph for the one folder.
"""

import logging
import threading
import uuid

from . import db
from .onedrive import GRAPH, OneDrive, OneDriveError, ResyncNeeded

log = logging.getLogger("garagebooks.onedrive")

FIRST_PAGE = (f"{GRAPH}/me/drive/root/delta"
              "?$select=id,name,size,folder,file,webUrl,parentReference,root,deleted&$top=1000")
MAX_PAGES = 2000        # in one run, about two million items
LEASE_SECONDS = 300     # a run that saves no page for this long is taken to have died
ON_OPEN_SECONDS = 10    # someone starting to browse gets a check for changes, unless one ran this recently
MAX_AGE_SECONDS = 300   # a click gets one when the last is this old
RETRY_SECONDS = 300     # after a failure
MAX_DEPTH = 100         # folders above one another


def wipe(conn):
    """Forget the listing (disconnected, or another registration): a sync that is running stops writing."""
    conn.execute("DELETE FROM garagebooks.drive_sync")
    conn.execute("DELETE FROM garagebooks.drive_item")


def in_background(work):
    threading.Thread(target=work, daemon=True, name="drive-sync").start()


def _state(conn):
    """The sync row, if it belongs to the current sign-in, with how long ago it synced and failed (seconds)."""
    return conn.execute(
        "SELECT s.*, EXTRACT(EPOCH FROM now() - s.synced_at) AS synced_ago,"
        " EXTRACT(EPOCH FROM now() - s.failed_at) AS failed_ago"
        " FROM garagebooks.drive_sync s JOIN garagebooks.onedrive_account a"
        " ON a.signed_in_at IS NOT DISTINCT FROM s.signed_in_at").fetchone()


def _save(conn, changes):
    """Apply one page of delta. An item can appear more than once: the last one counts."""
    latest = {c["id"]: c for c in changes if c.get("id")}
    keep, gone = [], []
    for fid, c in latest.items():
        if "deleted" in c or not ("folder" in c or OneDrive._is_listed_pdf(c)):
            gone.append(fid)
        else:
            parent = None if "root" in c else (c.get("parentReference") or {}).get("id")
            keep.append((fid, parent, c.get("name", ""), "folder" in c, c.get("size", 0), c.get("webUrl")))
    if gone:
        conn.execute("DELETE FROM garagebooks.drive_item WHERE id = ANY(%s)", (gone,))
    if keep:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO garagebooks.drive_item (id, parent_id, name, is_folder, size, web_url)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET parent_id = EXCLUDED.parent_id,"
                " name = EXCLUDED.name, is_folder = EXCLUDED.is_folder, size = EXCLUDED.size,"
                " web_url = EXCLUDED.web_url", keep)


class DriveStore:
    def __init__(self, connect, drive, access_token, start=None):
        self.connect, self.drive, self.access_token = connect, drive, access_token
        self.start = start or in_background
        self.running = threading.Lock()      # this worker's; across workers it is the lease in the database

    # ---- syncing --------------------------------------------------------------

    def _claim(self, mine):
        """Take the sync for this run. -> the sync row, or None when another run holds it."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM garagebooks.drive_sync s USING garagebooks.onedrive_account a"
                " WHERE a.signed_in_at IS DISTINCT FROM s.signed_in_at")
            if conn.execute("SELECT 1 FROM garagebooks.drive_sync").fetchone() is None:
                conn.execute("DELETE FROM garagebooks.drive_item")
                conn.execute("INSERT INTO garagebooks.drive_sync (signed_in_at)"
                             " SELECT signed_in_at FROM garagebooks.onedrive_account ON CONFLICT (id) DO NOTHING")
            return conn.execute(
                "UPDATE garagebooks.drive_sync SET lease_token = %s, lease_until = now() + make_interval(secs => %s)"
                " WHERE lease_until IS NULL OR lease_until < now() RETURNING cursor, complete",
                (mine, LEASE_SECONDS)).fetchone()

    def sync(self):
        """List the drive, carry on a listing, or apply what has changed since the last one. -> what was done."""
        mine = uuid.uuid4().hex
        row = self._claim(mine)
        if row is None:
            return "already running"
        url, complete, pages, count, resynced = row["cursor"] or FIRST_PAGE, row["complete"], 0, 0, False
        try:
            while pages < MAX_PAGES:
                try:
                    body = self.drive._get(self.access_token(), url)
                except ResyncNeeded:
                    if resynced:
                        raise
                    resynced, url, complete = True, FIRST_PAGE, False
                    with self.connect() as conn:
                        if not conn.execute("UPDATE garagebooks.drive_sync SET cursor = NULL, complete = false"
                                            " WHERE lease_token = %s RETURNING id", (mine,)).fetchone():
                            return "stopped: the listing was cleared"
                        conn.execute("DELETE FROM garagebooks.drive_item")
                    continue
                following = str(body.get("@odata.nextLink") or "")
                done = not following
                url = str(body.get("@odata.deltaLink") or "") if done else following
                if not url.startswith(GRAPH + "/"):      # the access token goes with it: only Graph is asked
                    raise OneDriveError("OneDrive didn't finish listing the drive.")
                changes = body.get("value", [])
                pages, count = pages + 1, count + len(changes)
                with self.connect() as conn:
                    # The row first: it is locked until the page is saved, and gone if the listing was cleared.
                    if not conn.execute(
                            "UPDATE garagebooks.drive_sync SET cursor = %(url)s, complete = %(complete)s,"
                            " synced_at = CASE WHEN %(done)s THEN now() ELSE synced_at END, failed_at = NULL,"
                            " lease_until = now() + make_interval(secs => %(lease)s)"
                            " WHERE lease_token = %(mine)s RETURNING id",
                            {"url": url, "complete": complete or done, "done": done, "lease": LEASE_SECONDS,
                             "mine": mine}).fetchone():
                        return "stopped: the listing was cleared"
                    _save(conn, changes)
                if done:
                    return f"{count} changes, {pages} pages"
            return f"{count} changes, {pages} pages: more to come"
        except Exception:
            with self.connect() as conn:
                conn.execute("UPDATE garagebooks.drive_sync SET failed_at = now() WHERE lease_token = %s", (mine,))
            raise
        finally:
            with self.connect() as conn:
                conn.execute("UPDATE garagebooks.drive_sync SET lease_token = NULL, lease_until = NULL"
                             " WHERE lease_token = %s", (mine,))

    def _sync_quietly(self):
        try:
            done = self.sync()
            log.log(logging.DEBUG if done == "already running" else logging.INFO, "drive listing synced: %s", done)
        except OneDriveError as exc:
            log.warning("drive listing not synced: %s", exc)
        except Exception:     # noqa: BLE001 - a background thread: say what happened rather than die silently
            log.exception("drive listing not synced")
        finally:
            self.running.release()

    def sync_soon(self):
        """Sync in the background, unless this worker already is."""
        if self.running.acquire(blocking=False):
            self.start(self._sync_quietly)

    # ---- reading --------------------------------------------------------------

    def browse(self, folder_id=None, fresh=False):
        """A folder as OneDrive.contents() gives it, from what is stored, or None when it isn't there to give
        (no complete listing yet, or a folder it doesn't hold). Never waits for Graph: any sync this calls for
        runs afterwards. `fresh`: someone has just started browsing."""
        with self.connect() as conn:
            state = _state(conn)
            found = self._read(conn, folder_id) if state and state["complete"] else None
        if state is None:
            due = True
        elif state["failed_ago"] is not None and state["failed_ago"] < RETRY_SECONDS:
            due = False
        elif not state["complete"] or found is None:
            due = True
        else:
            due = state["synced_ago"] >= (ON_OPEN_SECONDS if fresh else MAX_AGE_SECONDS)
        if due:
            self.sync_soon()
        return found

    @staticmethod
    def _read(conn, folder_id):
        where = "id = %s" if folder_id else "parent_id IS NULL AND %s::text IS NULL"
        here = conn.execute(f"SELECT id, parent_id, name FROM garagebooks.drive_item WHERE is_folder AND {where}",  # nosec B608 - one of two fixed clauses
                            (folder_id,)).fetchone()
        if here is None:
            return None
        names = conn.execute(
            "WITH RECURSIVE up AS (SELECT id, parent_id, name, 0 AS depth FROM garagebooks.drive_item WHERE id = %s"
            " UNION ALL SELECT i.id, i.parent_id, i.name, up.depth + 1 FROM garagebooks.drive_item i"
            " JOIN up ON i.id = up.parent_id WHERE up.depth < %s)"
            " SELECT name FROM up WHERE parent_id IS NOT NULL ORDER BY depth DESC", (here["id"], MAX_DEPTH)).fetchall()
        inside = conn.execute("SELECT id, name, is_folder, size, web_url FROM garagebooks.drive_item"
                              " WHERE parent_id = %s ORDER BY lower(name)", (here["id"],)).fetchall()
        info = {"id": here["id"], "name": here["name"], "path": "/" + "/".join(r["name"] for r in names),
                "parent_id": here["parent_id"], "is_root": here["parent_id"] is None}
        return (info, [{"id": r["id"], "name": r["name"]} for r in inside if r["is_folder"]],
                [{"id": r["id"], "name": r["name"], "size": r["size"], "web_url": r["web_url"]}
                 for r in inside if not r["is_folder"]])

    def add_folder(self, folder):
        """A folder the app has just made ({id, name, parent_id}), so it is listed before the next sync."""
        with self.connect() as conn:
            if _state(conn) and folder.get("parent_id"):
                _save(conn, [{"id": folder["id"], "name": folder["name"], "folder": {},
                              "parentReference": {"id": folder["parent_id"]}}])

    @staticmethod
    def connected(conn):
        account = db.onedrive_account(conn)
        return bool(account and account["refresh_token"])
