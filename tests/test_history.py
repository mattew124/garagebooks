"""Change history: a trigger in the database logs every add, edit and delete of an expense or
category (so changes made by other tools are caught too), and /history shows it."""

import re

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, db, validation
from tests.test_app import ORIGIN, category_id, form
from tests.test_upgrade import old_install

NEW = ("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor) "
       "VALUES (1, %s, '2026-11-07 04:00+00', 100, 'Shop') RETURNING id")


def log(admin):
    return admin.execute("SELECT * FROM garagebooks.change_log ORDER BY id").fetchall()


def plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ---- the trigger ---------------------------------------------------------------

def test_an_expense_is_logged_when_added_edited_and_deleted(admin):
    eid = admin.execute(NEW, (category_id(admin, "Tyres"),)).fetchone()["id"]
    admin.execute("UPDATE garagebooks.expense SET amount = 150, vendor = 'Other' WHERE id = %s", (eid,))
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (eid,))
    added, edited, deleted = log(admin)
    assert (added["operation"], added["table_name"], added["row_id"]) == ("insert", "expense", eid)
    assert added["before_row"] is None and added["after_row"]["vendor"] == "Shop"
    assert edited["operation"] == "update"
    assert (edited["before_row"]["amount"], edited["after_row"]["amount"]) == (100, 150)
    assert edited["before_row"]["vendor"] == "Shop" and edited["after_row"]["vendor"] == "Other"
    assert (deleted["operation"], deleted["after_row"]) == ("delete", None)
    assert deleted["before_row"]["amount"] == 150
    assert added["changed_at"] is not None


def test_a_category_is_logged_when_added_edited_and_deleted(admin):
    cid = admin.execute("INSERT INTO garagebooks.category (name, sort_order) VALUES ('Servicing', 100) "
                        "RETURNING id").fetchone()["id"]
    admin.execute("UPDATE garagebooks.category SET upload_folder = 'Maintenance' WHERE id = %s", (cid,))
    admin.execute("DELETE FROM garagebooks.category WHERE id = %s", (cid,))
    rows = log(admin)
    assert [(r["table_name"], r["operation"], r["row_id"]) for r in rows] == [
        ("category", "insert", cid), ("category", "update", cid), ("category", "delete", cid)]
    assert rows[1]["before_row"]["upload_folder"] is None and rows[1]["after_row"]["upload_folder"] == "Maintenance"


def test_an_update_that_changes_nothing_is_not_logged(admin):
    eid = admin.execute(NEW, (category_id(admin, "Tyres"),)).fetchone()["id"]
    admin.execute("UPDATE garagebooks.expense SET amount = amount WHERE id = %s", (eid,))
    admin.execute("UPDATE garagebooks.expense SET vendor = 'Shop', updated_at = now() + interval '1 hour' "
                  "WHERE id = %s", (eid,))   # the app always stamps updated_at; that alone is not a change
    assert [r["operation"] for r in log(admin)] == ["insert"]


def test_the_actor_is_read_from_the_transaction(admin):
    with admin.transaction():
        admin.execute("SELECT set_config('garagebooks.actor', 'alex@example.test', true)")
        admin.execute(NEW, (category_id(admin, "Tyres"),))
    admin.execute(NEW, (category_id(admin, "Tyres"),))          # the setting ended with the transaction
    named, anonymous = log(admin)
    assert named["actor"] == "alex@example.test" and anonymous["actor"] is None


def test_a_blank_actor_is_stored_as_unknown(admin):
    with admin.transaction():
        admin.execute("SELECT set_config('garagebooks.actor', '', true)")
        admin.execute(NEW, (category_id(admin, "Tyres"),))
    assert log(admin)[0]["actor"] is None


def test_the_log_cannot_be_changed_even_by_the_owner(admin):
    admin.execute(NEW, (category_id(admin, "Tyres"),))
    for sql in ("UPDATE garagebooks.change_log SET actor = 'me'", "DELETE FROM garagebooks.change_log",
                "TRUNCATE garagebooks.change_log"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            admin.execute(sql)
    assert len(log(admin)) == 1


def test_the_schema_can_be_applied_again_and_keeps_the_log(admin):
    admin.execute(NEW, (category_id(admin, "Tyres"),))
    admin.execute(db.SCHEMA_SQL.read_text())
    admin.execute(db.SCHEMA_SQL.read_text())
    assert len(log(admin)) == 1
    admin.execute(NEW, (category_id(admin, "Tyres"),))
    assert len(log(admin)) == 2          # one trigger, not two


# ---- upgrade, and the least-privilege role -------------------------------------

@pytest.mark.teslamate   # the old schema pointed at TeslaMate's cars
def test_an_old_install_starts_logging_after_the_upgrade(blank_db, monkeypatch):
    old_install(blank_db)
    monkeypatch.setenv("DATABASE_URL", blank_db)
    create_app()
    with psycopg.connect(blank_db, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM garagebooks.change_log").fetchone()[0] == 0   # no past invented
        conn.execute("UPDATE garagebooks.expense SET vendor = 'New shop'")
        assert conn.execute("SELECT before_row->>'vendor', after_row->>'vendor' FROM garagebooks.change_log"
                            ).fetchone() == ("Old shop", "New shop")


def test_the_least_privilege_role_logs_and_reads_but_cannot_rewrite_history(pg, admin):
    with psycopg.connect(pg.app_url, autocommit=True) as conn:
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) "
                     "VALUES (1, %s, now(), 5)", (category_id(admin, "Tyres"),))
        assert conn.execute("SELECT count(*) FROM garagebooks.change_log").fetchone()[0] == 1
        for sql in ("UPDATE garagebooks.change_log SET actor = 'x'", "DELETE FROM garagebooks.change_log",
                    "TRUNCATE garagebooks.change_log", "DROP TABLE garagebooks.change_log"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


# ---- who: the identity header --------------------------------------------------

def test_header_values_are_cleaned():
    assert validation.parse_actor("  alex@example.test ") == "alex@example.test"
    assert validation.parse_actor("a\x00b\r\nc\x7fd") == "abcd"
    assert validation.parse_actor("x" * 500) == "x" * validation.ACTOR_MAX
    assert validation.parse_actor("   ") is None and validation.parse_actor(None) is None


def make_client(pg, monkeypatch, header=None):
    monkeypatch.delenv("BASE_PATH", raising=False)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    if header:
        monkeypatch.setenv("AUTH_USER_HEADER", header)
    else:
        monkeypatch.delenv("AUTH_USER_HEADER", raising=False)
    app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row))
    app.testing = True
    return app.test_client()


def test_the_signed_in_user_is_recorded_for_every_kind_of_write(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch, "X-Authentik-Email")
    who = {**ORIGIN, "X-Authentik-Email": "alex@example.test"}
    assert client.post("/new", data=form(admin), headers=who).status_code == 303
    eid = admin.execute("SELECT id FROM garagebooks.expense").fetchone()["id"]
    assert client.post(f"/{eid}/edit", data=form(admin, amount="1"), headers=who).status_code == 303
    assert client.post("/categories", data={"name": "Servicing"}, headers=who).status_code == 303
    assert client.post(f"/{eid}/delete", headers=who).status_code == 303
    rows = log(admin)
    assert [(r["table_name"], r["operation"]) for r in rows] == [
        ("expense", "insert"), ("expense", "update"), ("category", "insert"), ("expense", "delete")]
    assert {r["actor"] for r in rows} == {"alex@example.test"}


def test_without_the_header_the_actor_is_unknown(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch, "X-Authentik-Email")
    client.post("/new", data=form(admin), headers=ORIGIN)
    assert log(admin)[0]["actor"] is None


def test_the_header_is_ignored_unless_the_setting_names_it(pg, admin, monkeypatch):
    """Anyone who can reach the app directly could send it, so nothing trusts it by default."""
    client = make_client(pg, monkeypatch)
    client.post("/new", data=form(admin), headers={**ORIGIN, "X-Authentik-Email": "mallory@example.test"})
    assert log(admin)[0]["actor"] is None


def test_a_hostile_header_value_is_trimmed_and_cannot_break_the_page(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch, "X-User")
    client.post("/new", data=form(admin), headers={**ORIGIN, "X-User": "<script>alert(1)</script>" + "z" * 400})
    assert len(log(admin)[0]["actor"]) == validation.ACTOR_MAX
    html = client.get("/history").data.decode()
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# ---- the page ------------------------------------------------------------------

def test_the_page_is_linked_from_the_navigation_and_empty_at_first(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    assert 'href="/history"' in client.get("/").data.decode()
    assert "No changes recorded" in client.get("/history").data.decode()


def test_the_page_shows_what_changed_newest_first(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch, "X-User")
    who = {**ORIGIN, "X-User": "alex@example.test"}
    client.post("/new", data=form(admin, vendor="First shop"), headers=who)
    eid = admin.execute("SELECT id FROM garagebooks.expense").fetchone()["id"]
    client.post(f"/{eid}/edit", data=form(admin, vendor="Second shop", amount="99"), headers=who)
    admin.execute("DELETE FROM garagebooks.expense")                      # by another tool: no actor
    text = plain(client.get("/history").data.decode().split('<ol class="plain history">')[1])   # past the filter's own words
    assert text.index("Deleted") < text.index("Edited") < text.index("Added")
    assert "alex@example.test" in text and "unknown" in text
    assert "Vendor First shop → Second shop" in text
    assert "Amount $1,200.50 → $99.00" in text
    edited = text.split("Edited")[1].split("Added")[0]
    assert "Odometer" not in edited and "Notes" not in edited              # unchanged fields are left out
    assert "Tyres" in text and "Test car" in text                          # names, not ids


def test_an_added_or_deleted_row_is_shown_in_full(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    client.post("/new", data=form(admin, vendor="Tyre Shop", notes="Four new ones"), headers=ORIGIN)
    text = plain(client.get("/history").data.decode().split('<ol class="plain history">')[1])
    for expected in ("Tyre Shop", "Four new ones", "$1,200.50", "57,000 km", "Tyres", "Test car", "07 Nov 2026 14:05"):
        assert expected in text


def test_filters(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    client.post("/new", data=form(admin, vendor="Alpha"), headers=ORIGIN)
    client.post("/categories", data={"name": "Servicing"}, headers=ORIGIN)
    admin.execute("INSERT INTO garagebooks.car (id, name) VALUES (2, 'Second car')")
    try:
        admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor) "
                      "VALUES (2, %s, now(), 7, 'Beta')", (category_id(admin, "Tyres"),))
        admin.execute("DELETE FROM garagebooks.expense WHERE vendor = 'Beta'")

        def page(**q):
            return client.get("/history", query_string=q).data.decode()
        assert "Servicing" not in page(table="expense") and "Alpha" in page(table="expense")
        assert "Servicing" in page(table="category") and "Alpha" not in page(table="category")
        assert "Beta" in page(operation="delete") and "Alpha" not in page(operation="delete")
        second = page(car=2)
        assert "Beta" in second and "Alpha" not in second and "Servicing" not in second
        assert "Alpha" in page(car=1) and "Beta" not in page(car=1)
        assert "Alpha" in page(table="nonsense", operation="x", car="y", page="z")   # bad filters are ignored
    finally:
        admin.execute("TRUNCATE garagebooks.expense CASCADE; DELETE FROM garagebooks.car WHERE id = 2")


def test_the_log_is_paginated(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor) "
                  "SELECT 1, %s, now(), n, 'Row ' || n FROM generate_series(1, 120) n", (category_id(admin, "Tyres"),))
    first = client.get("/history").data.decode()
    assert first.count('<li class="change ') == 50
    assert "Row 120" in first and "Row 70" not in first          # newest first
    assert "page=2" in first
    third = client.get("/history?page=3").data.decode()
    assert third.count('<li class="change ') == 20 and "Row 1<" in third and "page=2" in third
    assert client.get("/history?page=99").data.decode().count('<li class="change ') == 20   # clamped to the last
    filtered = client.get("/history?operation=insert&page=2").data.decode()
    assert "operation=insert" in filtered and "page=3" in filtered   # filters ride along


def test_everything_shown_is_escaped(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    client.post("/new", data=form(admin, vendor="<img src=x onerror=alert(1)>", notes="<b>bold</b>"), headers=ORIGIN)
    client.post("/categories", data={"name": "<i>odd</i>"}, headers=ORIGIN)
    html = client.get("/history").data.decode()
    assert "<img src=x" not in html and "<b>bold</b>" not in html and "<i>odd</i>" not in html
    assert "&lt;img src=x" in html


def test_the_page_is_read_only(pg, admin, monkeypatch):
    client = make_client(pg, monkeypatch)
    assert client.post("/history", headers=ORIGIN).status_code == 405
