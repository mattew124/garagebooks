"""How many Microsoft Graph calls a scan makes.

A scan of a car's OneDrive folder lists every PDF in it and its subfolders. One `/children` call per folder
(and per page) is a call per folder; `driveItem: delta` answers for the whole subtree in a few pages. The mailbox
scan is one `/messages` call per 50 emails, with their attachments expanded, which a delta query can't do.
These tests count the calls with a fake transport, so a change that makes a scan chattier fails here.
"""

import math
import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from garagebooks import onedrive, outlook
from tests.test_onedrive import KEY

CAR = "CAR"
# id -> (name, parent id). Nested folders; "Deep" is five levels below the car's folder, past the search depth.
FOLDERS = {"CAR": ("Car", "ROOT"), "Y22": ("2022", "CAR"), "Y23": ("2023", "CAR"), "REC": ("Receipts", "Y23"),
           "INS": ("Insurance", "CAR"), "D1": ("d1", "CAR"), "D2": ("d2", "D1"), "D3": ("d3", "D2"),
           "D4": ("d4", "D3"), "D5": ("d5", "D4")}
FILES = [("F1", "tyres.pdf", "CAR"), ("F2", "notes.txt", "CAR"), ("F3", "service.pdf", "Y22"),
         ("F4", "rego.PDF", "Y23"), ("F5", "fuel.pdf", "REC"), ("F6", "policy.pdf", "INS"),
         ("F7", "ok.pdf", "D4"), ("F8", "too-deep.pdf", "D5")]


class Drive:
    """Graph for a drive: answers /children and /delta for the tree above, and counts what was asked."""

    def __init__(self, delta=True, delta_page=4, files=FILES):
        self.asked, self.delta, self.delta_page, self.files = [], delta, delta_page, list(files)

    def _folder(self, fid):
        name, parent = FOLDERS[fid]
        return {"id": fid, "name": name, "folder": {}, "parentReference": {"id": parent}}

    def _file(self, fid, name, parent):
        return {"id": fid, "name": name, "file": {}, "size": 10, "webUrl": f"https://onedrive.live.com/?id={fid}",
                "parentReference": {"id": parent}}

    def __call__(self, method, url, data=None, headers=None, raw=False):
        self.asked.append(url)
        parsed = urlparse(url)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path.endswith("/children"):
            fid = path.split("/items/")[1].split("/")[0]
            kids = [self._folder(k) for k, (_, p) in FOLDERS.items() if p == fid]
            kids += [self._file(*f) for f in self.files if f[2] == fid]
            return 200, {"value": kids}
        if path.endswith("/delta"):
            if not self.delta:
                return 400, {"error": {"code": "invalidRequest", "message": "delta isn't offered here"}}
            fid = path.split("/items/")[1].split("/")[0]
            assert "token" in query or "$select" in query, "delta is asked for only the fields the scan uses"
            inside = {fid}
            while True:         # every folder below the one asked for
                more = {k for k, (_, p) in FOLDERS.items() if p in inside} - inside
                if not more:
                    break
                inside |= more
            everything = [{"id": "GONE", "deleted": {}, "parentReference": {"id": fid}}]
            everything += [self._file(*f) for f in self.files if f[2] in inside]        # children before parents
            everything += [self._folder(k) for k in sorted(inside, reverse=True)]
            page = int(query.get("token", ["0"])[0])
            chunk = everything[page * self.delta_page:(page + 1) * self.delta_page]
            body = {"value": chunk}
            if (page + 1) * self.delta_page < len(everything):
                body["@odata.nextLink"] = f"{onedrive.GRAPH}/me/drive/items/{fid}/delta?token={page + 1}"
            else:
                body["@odata.deltaLink"] = f"{onedrive.GRAPH}/me/drive/items/{fid}/delta?token=done"
            return 200, body
        raise AssertionError(f"unexpected call {method} {url}")


def scan(drive):
    return onedrive.OneDrive(KEY, drive).pdfs("AT-1", CAR)


EXPECTED = [("", "tyres.pdf"), ("2022", "service.pdf"), ("2023", "rego.PDF"), ("2023/Receipts", "fuel.pdf"),
            ("Insurance", "policy.pdf"), ("d1/d2/d3/d4", "ok.pdf")]


def names(found):
    return sorted((f["folder"], f["name"]) for f in found)


def test_a_scan_finds_the_pdfs_below_the_folder_to_the_search_depth():
    found = scan(Drive())
    assert names(found) == sorted(EXPECTED)
    assert all(f["web_url"].startswith("https://") and f["size"] == 10 for f in found)
    assert [(f["folder"].lower(), f["name"].lower()) for f in found] == sorted(
        (f["folder"].lower(), f["name"].lower()) for f in found)


def test_a_scan_of_a_folder_tree_is_a_few_delta_calls_not_one_call_per_folder():
    drive = Drive(delta_page=200)
    scan(drive)
    assert len(drive.asked) == 1, drive.asked
    assert "/delta" in drive.asked[0] and "children" not in drive.asked[0]


def test_delta_follows_its_next_pages_and_nothing_else():
    drive = Drive(delta_page=4)
    scan(drive)
    assert len(drive.asked) == math.ceil((len(FOLDERS) + len(FILES) + 1) / 4) == 5
    assert all(u.startswith(onedrive.GRAPH + "/") for u in drive.asked)
    assert not any("deltaLink" in u or "token=done" in u for u in drive.asked), "the delta link isn't followed"


def test_a_folder_that_cannot_be_delta_listed_falls_back_to_children_with_the_same_answer():
    drive = Drive(delta=False)
    found = scan(drive)
    assert names(found) == sorted(EXPECTED)
    assert "/delta" in drive.asked[0]
    assert len([u for u in drive.asked if "/children" in u]) == 9      # the folders within the search depth



def test_a_delta_page_off_graph_is_not_followed():
    def transport(method, url, data=None, headers=None, raw=False):
        return 200, {"value": [], "@odata.nextLink": "https://example.com/steal"}
    drive = onedrive.OneDrive(KEY, transport)
    assert drive.pdfs("AT-1", CAR) == []


# ---- the mailbox ---------------------------------------------------------------------------------------

def test_a_mailbox_scan_is_one_call_per_page_of_emails_with_attachments_included():
    emails = [{"id": f"M{i}", "subject": f"Invoice {i}", "webLink": f"https://outlook.example/m{i}",
               "receivedDateTime": "2026-10-25T01:00:00Z",
               "attachments": [{"id": "A", "name": "inv.pdf", "size": 5}]} for i in range(120)]
    asked = []

    def transport(method, url, data=None, headers=None, raw=False):
        asked.append(url)
        offset = int(re.search(r"skip=(\d+)", url).group(1)) if "skip=" in url else 0
        body = {"value": emails[offset:offset + outlook.PAGE]}
        if offset + outlook.PAGE < len(emails):
            body["@odata.nextLink"] = f"{onedrive.GRAPH}/me/mailFolders/F/messages?skip={offset + outlook.PAGE}"
        return 200, body

    found = outlook.Mailbox(onedrive.OneDrive(KEY, transport)).pdfs("AT-1", "F")
    assert len(found) == 120
    assert len(asked) == math.ceil(120 / outlook.PAGE) == 3
    assert "expand=attachments" in asked[0] and "/delta" not in asked[0], \
        "mail delta can't expand attachments, so it would need a call per email as well"
