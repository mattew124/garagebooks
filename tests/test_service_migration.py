"""The old service-type categories (Cabin air filter, Air filter, Wheel alignment, Tyre rotation) became items of the
Service category. Starting the app on a database that still has them moves everything over, once, and loses nothing.
Reminders work at the service-item level too. Runs with TeslaMate present and absent (blank_db)."""

from pathlib import Path

import psycopg
import pytest

from garagebooks import create_app

OLD = {"Cabin air filter": "Cabin air filter", "Air filter": "Air filter", "Wheel alignment": "Wheel alignment",
       "Tyre rotation": "Tyre rotation"}


def started(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    create_app()
    return psycopg.connect(url, autocommit=True)


def old_world(conn):
    """What the database held before this release: the four categories, with expenses, a reminder, an upload folder
    and learned vendors in them. -> {category name: id}"""
    ids = {}
    conn.execute("COMMENT ON TABLE garagebooks.service_item IS NULL")       # as a database from before this release is
    for order, name in enumerate(OLD, start=50):
        ids[name] = conn.execute("INSERT INTO garagebooks.category (name, kind, sort_order, upload_folder, budget_pool) "
                                 "VALUES (%s, 'running', %s, %s, %s) RETURNING id",
                                 (name, order, "Maintenance" if name != "Air filter" else None,
                                  "tyres" if name in ("Wheel alignment", "Tyre rotation") else "maintenance")).fetchone()[0]
    return ids


def populate(conn, ids):
    car = conn.execute("SELECT id FROM garagebooks.car ORDER BY id LIMIT 1").fetchone()[0]
    for name, cid in ids.items():
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor, notes) "
                     "VALUES (%s, %s, '2026-05-01 02:00+00', 100, 'Example Auto', %s)", (car, cid, f"old {name}"))
        conn.execute("INSERT INTO garagebooks.reminder (car_id, title, category_id, due_date, every_months) "
                     "VALUES (%s, %s, %s, '2027-01-01', 12)", (car, f"{name} due", cid))
    conn.execute("INSERT INTO garagebooks.learned_vendor (vendor, category_id, hits) VALUES ('example auto', %s, 3)",
                 (ids["Air filter"],))
    conn.execute("INSERT INTO garagebooks.learned_vendor (vendor, category_id, hits) VALUES ('example auto', %s, 2)",
                 (ids["Cabin air filter"],))
    return car


def test_a_fresh_database_has_no_service_type_categories(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM garagebooks.category")}
        assert names.isdisjoint(OLD) and {"Tyres", "Service", "Insurance", "Registration", "Initial purchase"} <= names
        items = {r[0] for r in conn.execute("SELECT name FROM garagebooks.service_item")}
        assert set(OLD) <= items                                    # each is an item now, Wheel alignment included


def test_the_old_categories_move_into_service_with_their_items_ticked(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        ids = old_world(conn)
        conn.execute("DELETE FROM garagebooks.service_item WHERE name = 'Wheel alignment'")     # an item to be added back
        populate(conn, ids)
    with started(blank_db, monkeypatch) as conn:
        service = conn.execute("SELECT id FROM garagebooks.category WHERE name = 'Service'").fetchone()[0]
        got = conn.execute("SELECT e.notes, e.category_id, i.name, e.vendor, e.amount FROM garagebooks.expense e "
                           "JOIN garagebooks.expense_service_item l ON l.expense_id = e.id "
                           "JOIN garagebooks.service_item i ON i.id = l.item_id ORDER BY e.id").fetchall()
        assert [(n, c == service, i) for n, c, i, _, _ in got] == [(f"old {name}", True, name) for name in OLD]
        assert all(v == "Example Auto" and float(a) == 100 for *_, v, a in got)
        assert conn.execute("SELECT count(*) FROM garagebooks.expense").fetchone()[0] == 4          # nothing lost
        left = {r[0] for r in conn.execute("SELECT name FROM garagebooks.category")}
        assert left.isdisjoint(OLD)                                                                # deleted: nothing refers to them


def test_reminders_move_to_the_matching_item(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        populate(conn, old_world(conn))
    with started(blank_db, monkeypatch) as conn:
        service = conn.execute("SELECT id FROM garagebooks.category WHERE name = 'Service'").fetchone()[0]
        got = conn.execute("SELECT r.title, r.category_id, i.name FROM garagebooks.reminder r "
                           "JOIN garagebooks.service_item i ON i.id = r.service_item_id ORDER BY r.id").fetchall()
        assert got == [(f"{name} due", service, name) for name in OLD]
        due = conn.execute("SELECT title, service_item_id FROM garagebooks.reminder_due WHERE kind = 'reminder'").fetchall()
        assert len(due) == 4 and all(item is not None for _, item in due)


def test_the_upload_folder_and_budget_pool_follow(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        populate(conn, old_world(conn))
        conn.execute("UPDATE garagebooks.category SET upload_folder = NULL WHERE name = 'Service'")
    with started(blank_db, monkeypatch) as conn:
        folder, pool = conn.execute("SELECT upload_folder, budget_pool FROM garagebooks.category WHERE name = 'Service'").fetchone()
        assert (folder, pool) == ("Maintenance", "maintenance")           # Service had none, so it takes the old folder


def test_an_upload_folder_the_owner_gave_service_is_kept(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        populate(conn, old_world(conn))
        conn.execute("UPDATE garagebooks.category SET upload_folder = 'Servicing' WHERE name = 'Service'")
    with started(blank_db, monkeypatch) as conn:
        assert conn.execute("SELECT upload_folder FROM garagebooks.category WHERE name = 'Service'").fetchone()[0] == "Servicing"


def test_learned_vendors_are_merged_not_lost(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        populate(conn, old_world(conn))
    with started(blank_db, monkeypatch) as conn:
        got = conn.execute("SELECT l.vendor, c.name, l.hits FROM garagebooks.learned_vendor l "
                           "JOIN garagebooks.category c ON c.id = l.category_id").fetchall()
        assert got == [("example auto", "Service", 5)]


def test_a_category_the_owner_adds_after_the_move_is_theirs(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:       # a first start does the move (there is nothing to move)
        conn.execute("INSERT INTO garagebooks.category (name, sort_order) VALUES ('Air filter', 100)")
    with started(blank_db, monkeypatch) as conn:
        assert conn.execute("SELECT count(*) FROM garagebooks.category WHERE name = 'Air filter'").fetchone()[0] == 1
        conn.execute("DELETE FROM garagebooks.category WHERE name = 'Air filter'")


def test_the_move_runs_once_and_changes_nothing_when_run_again(blank_db, monkeypatch):
    with started(blank_db, monkeypatch) as conn:
        populate(conn, old_world(conn))
    with started(blank_db, monkeypatch) as conn:
        before = conn.execute("SELECT count(*) FROM garagebooks.change_log").fetchone()[0]
        snapshot = conn.execute("SELECT id, category_id FROM garagebooks.expense ORDER BY id").fetchall()
    with started(blank_db, monkeypatch) as conn:
        assert conn.execute("SELECT id, category_id FROM garagebooks.expense ORDER BY id").fetchall() == snapshot
        assert conn.execute("SELECT count(*) FROM garagebooks.change_log").fetchone()[0] == before


def test_a_category_still_in_use_elsewhere_is_not_deleted(blank_db, monkeypatch):
    """A new table of someone's own pointing at the old category (nothing in this app does) must stop the delete."""
    with started(blank_db, monkeypatch) as conn:
        ids = old_world(conn)
        conn.execute("CREATE TABLE public.zz_mine (category_id smallint REFERENCES garagebooks.category (id))")
        conn.execute("INSERT INTO public.zz_mine VALUES (%s)", (ids["Air filter"],))
    with started(blank_db, monkeypatch) as conn:
        assert conn.execute("SELECT count(*) FROM garagebooks.category WHERE name = 'Air filter'").fetchone()[0] == 1
        conn.execute("DROP TABLE public.zz_mine")


# ---- the real 0.4.0 upgrade path, with the old seeds ------------------------------------------------

OLD_SCHEMA = (Path(__file__).parent / "fixtures" / "schema-0.4.0-rc1.sql").read_text()


@pytest.mark.teslamate
def test_an_install_from_the_old_release_ends_with_service_items(blank_db, monkeypatch):
    with psycopg.connect(blank_db, autocommit=True) as conn:
        conn.execute(OLD_SCHEMA)
        for name in OLD:
            cid = conn.execute("SELECT id FROM expenses.category WHERE name = %s", (name,)).fetchone()[0]
            conn.execute("INSERT INTO expenses.expense (car_id, category_id, occurred_at, amount, vendor) "
                         "VALUES (1, %s, now(), 50, %s)", (cid, name))
    monkeypatch.setenv("DATABASE_URL", blank_db)
    create_app()
    with psycopg.connect(blank_db) as conn:
        got = conn.execute("SELECT e.vendor, c.name, i.name FROM garagebooks.expense e "
                           "JOIN garagebooks.category c ON c.id = e.category_id "
                           "JOIN garagebooks.expense_service_item l ON l.expense_id = e.id "
                           "JOIN garagebooks.service_item i ON i.id = l.item_id ORDER BY e.id").fetchall()
        assert got == [(name, "Service", name) for name in OLD]
        assert conn.execute("SELECT count(*) FROM garagebooks.category WHERE name = ANY(%s)", (list(OLD),)).fetchone()[0] == 0
