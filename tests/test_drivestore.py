"""The OneDrive folder listing kept in the database, and how browsing reads it.

Browsing must never wait for a listing or for changes: it reads what is stored and syncs in the background.
"""

import http.client
import re
from urllib.parse import parse_qs, unquote, urlparse

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import db, drivestore, onedrive
from tests.test_onedrive import KEY


class WholeDrive:
    """Graph's delta for the drive root: the full listing in pages, then only what `changes` holds."""

    def __init__(self, page=3):
        self.asked, self.page, self.changes, self.fail = [], page, [], {}
        root = {"id": "R", "name": "root", "root": {}, "folder": {}}
        self.items = [self.pdf("P1", "b.pdf", "B"), self.pdf("P2", "A.pdf", "B"),
                      {"id": "TXT", "name": "n.txt", "file": {}, "parentReference": {"id": "B"}},
                      self.dir("B", "beta", "A"), self.dir("Z", "Zed", "R"), self.dir("A", "Alpha", "R"), root]

    @staticmethod
    def dir(fid, name, parent):
        return {"id": fid, "name": name, "folder": {}, "parentReference": {"id": parent}}

    @staticmethod
    def pdf(fid, name, parent):
        return {"id": fid, "name": name, "file": {}, "size": 10, "webUrl": f"https://onedrive.live.com/?id={fid}",
                "parentReference": {"id": parent}}

    def __call__(self, method, url, data=None, headers=None, raw=False):
        self.asked.append(url)
        parsed = urlparse(url)
        assert unquote(parsed.path).endswith("/me/drive/root/delta"), url
        token = parse_qs(parsed.query).get("token", ["0"])[0]
        if token in self.fail:
            return self.fail.pop(token)
        base = f"{onedrive.GRAPH}/me/drive/root/delta?token="
        if token == "since":
            changed, self.changes = self.changes, []
            return 200, {"value": changed, "@odata.deltaLink": base + "since"}
        n = int(token)
        body = {"value": self.items[n * self.page:(n + 1) * self.page]}
        if (n + 1) * self.page < len(self.items):
            body["@odata.nextLink"] = base + str(n + 1)
        else:
            body["@odata.deltaLink"] = base + "since"
        return 200, body


@pytest.fixture()
def connect(pg, admin):
    def make():
        return psycopg.connect(pg.app_url, row_factory=dict_row)
    with make() as conn:
        db.save_onedrive_account(conn, client_id="c", client_secret="s", refresh_token="r")
    try:
        yield make
    finally:
        admin.execute("DELETE FROM garagebooks.drive_item")
        admin.execute("DELETE FROM garagebooks.drive_sync")


def store(connect, graph, waiting=None):
    """A store whose background work runs at once, or is held in `waiting`."""
    return drivestore.DriveStore(connect, onedrive.OneDrive(KEY, graph), lambda: "AT-1",
                                 start=(lambda work: work()) if waiting is None else waiting.append)


def age(admin, seconds=3600):
    admin.execute("UPDATE garagebooks.drive_sync SET synced_at = synced_at - make_interval(secs => %s),"
                  " failed_at = failed_at - make_interval(secs => %s)", (seconds, seconds))


def test_the_first_browse_does_not_wait_for_the_listing(connect):
    graph, waiting = WholeDrive(), []
    assert store(connect, graph, waiting).browse(None, fresh=True) is None     # the caller asks Graph for one folder
    assert graph.asked == [] and len(waiting) == 1


def test_the_stored_listing_is_read_without_asking_graph_and_survives_a_restart(connect):
    graph = WholeDrive()
    store(connect, graph).sync()
    graph.asked.clear()
    again = store(connect, graph, [])                           # another worker, or the app restarted
    here, folders, files = again.browse(None)
    assert (here["id"], here["path"], here["is_root"], here["parent_id"]) == ("R", "/", True, None)
    assert [f["name"] for f in folders] == ["Alpha", "Zed"] and files == []
    here, folders, files = again.browse("B")
    assert here == {"id": "B", "name": "beta", "path": "/Alpha/beta", "parent_id": "A", "is_root": False}
    assert folders == [] and [f["name"] for f in files] == ["A.pdf", "b.pdf"]
    assert files[0] == {"id": "P2", "name": "A.pdf", "size": 10, "web_url": "https://onedrive.live.com/?id=P2"}
    assert graph.asked == []


def test_opening_a_picker_answers_from_the_database_and_checks_for_changes_afterwards(connect, admin):
    graph, waiting = WholeDrive(), []
    store(connect, graph).sync()
    age(admin)
    graph.asked.clear()
    graph.changes = [graph.dir("NEW", "New", "R"), graph.dir("B", "Beta 2", "Z"), {"id": "P1", "deleted": {}},
                     {"id": "P2", "name": "A.txt", "file": {}, "parentReference": {"id": "B"}}]
    lazy = store(connect, graph, waiting)
    assert [f["name"] for f in lazy.browse(None, fresh=True)[1]] == ["Alpha", "Zed"]    # at once, as stored
    assert graph.asked == [] and len(waiting) == 1
    waiting.pop()()
    assert len(graph.asked) == 1
    assert [f["name"] for f in lazy.browse(None)[1]] == ["Alpha", "New", "Zed"]
    here, _, files = lazy.browse("B")
    assert here["path"] == "/Zed/Beta 2" and files == []


def test_a_listing_cut_short_carries_on_from_the_last_page_saved(connect, admin):
    graph = WholeDrive()
    graph.fail["2"] = (500, {})
    first = store(connect, graph)
    with pytest.raises(onedrive.OneDriveError):
        first.sync()
    assert first.browse(None) is None                           # half a listing is never shown
    graph.asked.clear()
    store(connect, graph).sync()
    assert ["token=" + u.rsplit("token=", 1)[1] for u in graph.asked] == ["token=2"]
    assert store(connect, graph, []).browse("B")[2]


def test_a_failed_listing_is_not_started_again_on_every_click(connect, admin):
    graph, waiting = WholeDrive(), []
    graph.fail["0"] = (500, {})
    with pytest.raises(onedrive.OneDriveError):
        store(connect, graph).sync()
    lazy = store(connect, graph, waiting)
    lazy.browse(None)
    assert waiting == []
    age(admin)
    lazy.browse(None)
    assert len(waiting) == 1


def test_an_expired_change_marker_lists_the_drive_again(connect):
    graph = WholeDrive()
    s = store(connect, graph)
    s.sync()
    graph.items = [i for i in graph.items if i["id"] != "Z"]
    graph.fail["since"] = (410, {"error": {"code": "resyncRequired"}})
    s.sync()
    assert [f["name"] for f in s.browse(None)[1]] == ["Alpha"]


def test_only_one_sync_runs_at_a_time(connect, admin):
    graph = WholeDrive()
    admin.execute("INSERT INTO garagebooks.drive_sync (lease_token, lease_until) VALUES ('other', now() + interval '1 minute')")
    assert store(connect, graph).sync() == "already running"
    assert graph.asked == []


def test_a_new_sign_in_starts_the_listing_again(connect):
    graph, waiting = WholeDrive(), []
    store(connect, graph).sync()
    with connect() as conn:
        conn.execute("UPDATE garagebooks.onedrive_account SET signed_in_at = now()")
    assert store(connect, graph, waiting).browse(None) is None
    graph.asked.clear()
    store(connect, graph).sync()
    assert "token=" not in graph.asked[0]


def test_a_sync_stops_writing_once_the_listing_is_wiped(connect, admin):
    """Disconnecting while a listing runs must leave nothing behind."""
    graph = WholeDrive()
    real = graph.__call__

    def disconnect_after_the_first_page(*a, **k):
        if len(graph.asked) == 1:
            with connect() as conn:
                drivestore.wipe(conn)
        return real(*a, **k)
    store(connect, disconnect_after_the_first_page).sync()
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.drive_item").fetchone()["n"] == 0


def test_a_next_page_off_graph_is_not_followed(connect):
    def transport(method, url, data=None, headers=None, raw=False):
        return 200, {"value": [], "@odata.nextLink": "https://example.com/steal"}
    with pytest.raises(onedrive.OneDriveError):
        store(connect, transport).sync()


def test_a_folder_the_app_creates_is_there_at_once(connect):
    graph = WholeDrive()
    s = store(connect, graph)
    s.sync()
    s.add_folder({"id": "MADE", "name": "Made", "parent_id": "Z"})
    assert [f["name"] for f in store(connect, graph, []).browse("Z")[1]] == ["Made"]


# ---- a dead address must not hold a request for a minute --------------------------------------------

def test_connecting_gives_up_quickly_and_reading_still_has_a_minute(monkeypatch):
    seen = {}

    class Sock:
        def settimeout(self, value):
            seen["read"] = value

    def connect(self):
        seen["connect"] = self.timeout
        self.sock = Sock()
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    c = onedrive.QuickConnection("graph.microsoft.com", timeout=60)
    c.connect()
    assert seen == {"connect": onedrive.CONNECT_TIMEOUT, "read": 60}
    assert onedrive.CONNECT_TIMEOUT <= 5
