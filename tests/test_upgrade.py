"""Upgrading from a release that still used the old names (0.4.0-rc1 and earlier): the `expenses`
schema becomes `garagebooks` with everything in it, the setting `expenses_url` becomes `app_url`,
and a saved dashboard link follows the dashboard's new uid. Nothing else is touched."""

from decimal import Decimal
from pathlib import Path

import psycopg

from garagebooks import create_app, db

OLD_SCHEMA = (Path(__file__).parent / "fixtures" / "schema-0.4.0-rc1.sql").read_text()


def old_install(url):
    """A database as 0.4.0-rc1 left it, with one expense and both links saved."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(OLD_SCHEMA)
        conn.execute("UPDATE expenses.setting SET expenses_url = 'https://cars.example.test/expenses/', "
                     "grafana_url = 'https://cars.example.test/grafana/d/tm-expenses-tco/cost-of-ownership?orgId=1'")
        category = conn.execute("SELECT id FROM expenses.category WHERE name = 'Tyres'").fetchone()[0]
        conn.execute("INSERT INTO expenses.expense (car_id, category_id, occurred_at, amount, vendor) "
                     "VALUES (1, %s, now(), 123.45, 'Old shop')", (category,))


def schemas(conn):
    return {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace")}


def test_an_old_install_is_renamed_on_start_and_keeps_its_data(blank_db, monkeypatch):
    old_install(blank_db)
    monkeypatch.setenv("DATABASE_URL", blank_db)
    create_app()
    with psycopg.connect(blank_db) as conn:
        assert "garagebooks" in schemas(conn) and "expenses" not in schemas(conn)
        assert conn.execute("SELECT vendor, amount FROM garagebooks.expense").fetchall() == [("Old shop", Decimal("123.45"))]
        assert conn.execute("SELECT app_url, grafana_url FROM garagebooks.setting").fetchone() == (
            "https://cars.example.test/expenses/",          # the app's address is the owner's: kept
            "https://cars.example.test/grafana/d/garagebooks-tco/cost-of-ownership?orgId=1")
        assert conn.execute("SELECT count(*) FROM garagebooks.ownership").fetchone()[0] >= 1


def app_url(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT app_url FROM garagebooks.setting").fetchone()[0]


def test_the_upgrade_moves_the_saved_address_to_the_new_path(blank_db, monkeypatch):
    """Upgrading and moving to /garagebooks in one go needs nothing done by hand: the dashboard's
    link back to the app follows BASE_PATH, keeping the owner's scheme and host."""
    old_install(blank_db)
    monkeypatch.setenv("DATABASE_URL", blank_db)
    monkeypatch.setenv("BASE_PATH", "/garagebooks")
    create_app()
    assert app_url(blank_db) == "https://cars.example.test/garagebooks/"


def test_the_upgrade_keeps_the_address_when_the_path_did_not_change(blank_db, monkeypatch):
    old_install(blank_db)
    monkeypatch.setenv("DATABASE_URL", blank_db)
    monkeypatch.setenv("BASE_PATH", "/expenses")
    create_app()
    assert app_url(blank_db) == "https://cars.example.test/expenses/"


def test_the_upgrade_runs_once(blank_db, monkeypatch):
    old_install(blank_db)
    monkeypatch.setenv("DATABASE_URL", blank_db)
    monkeypatch.setenv("BASE_PATH", "/garagebooks")
    create_app()
    with psycopg.connect(blank_db, autocommit=True) as conn:  # the owner then sets an address of their own
        conn.execute("UPDATE garagebooks.setting SET app_url = 'https://other.example.test/books/'")
    create_app()                                            # a second start finds nothing to upgrade
    with psycopg.connect(blank_db) as conn:
        assert conn.execute("SELECT count(*) FROM garagebooks.expense").fetchone()[0] == 1
    assert app_url(blank_db) == "https://other.example.test/books/"


def test_an_unrelated_expenses_schema_is_left_alone(blank_db, monkeypatch):
    """Another app's `expenses` schema is not ours to rename: ours always has a `setting` table."""
    with psycopg.connect(blank_db, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA expenses; CREATE TABLE expenses.other (id int)")
    monkeypatch.setenv("DATABASE_URL", blank_db)
    create_app()
    with psycopg.connect(blank_db) as conn:
        assert {"expenses", "garagebooks"} <= schemas(conn)
        assert conn.execute("SELECT to_regclass('expenses.other') IS NOT NULL").fetchone()[0]


def test_the_dashboard_uid_has_no_old_name():
    assert db.DASHBOARD_UID == "garagebooks-tco"


def test_the_old_encryption_key_name_still_turns_onedrive_on(monkeypatch, caplog):
    from cryptography.fernet import Fernet
    monkeypatch.delenv("GARAGEBOOKS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("EXPENSES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with caplog.at_level("INFO"):
        create_app(connect=lambda: None)
    assert any("onedrive=on" in r.getMessage() for r in caplog.records)


def test_the_new_encryption_key_name_turns_onedrive_on(monkeypatch, caplog):
    from cryptography.fernet import Fernet
    monkeypatch.delenv("EXPENSES_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("GARAGEBOOKS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with caplog.at_level("INFO"):
        create_app(connect=lambda: None)
    assert any("onedrive=on" in r.getMessage() for r in caplog.records)
