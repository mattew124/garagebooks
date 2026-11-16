"""scripts/backup.sh: dump and restore only the `garagebooks` schema, and check an existing
TeslaMate backup for it. Each test works on its own copy of the throwaway database."""

import gzip
import os
import secrets
import subprocess
from decimal import Decimal

import psycopg
import pytest

from tests.test_dev_stack import ROOT

SCRIPT = ROOT / "scripts" / "backup.sh"
SCHEMA = (ROOT / "garagebooks" / "schema.sql").read_text()
KEY = "dGVzdC1rZXktdGhhdC1tdXN0LW5ldmVyLWFwcGVhci0xMjM0NTY="


def db_name(url):
    return url.rsplit("/", 1)[1]


def run(pg, *args, db, cwd):
    env = {**os.environ, "DB_CONTAINER": pg.container, "DB_USER": "postgres", "DB_NAME": db,
           "GARAGEBOOKS_ENCRYPTION_KEY": KEY}
    return subprocess.run([str(SCRIPT), *args], env=env, cwd=cwd, capture_output=True, text=True)


def install(url, vendor="Round trip shop"):
    """The garagebooks schema, as the app creates it, with one expense."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA)
        category = conn.execute("SELECT id FROM garagebooks.category WHERE name = 'Tyres'").fetchone()[0]
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor) "
                     "VALUES (1, %s, now(), 321.50, %s)", (category, vendor))


def expenses(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT vendor, amount FROM garagebooks.expense").fetchall()


def whole_database_dump(pg, url, *flags):
    return subprocess.run(["docker", "exec", pg.container, "pg_dump", "-U", "postgres", *flags, "-d", db_name(url)],
                          capture_output=True, check=True).stdout


@pytest.fixture()
def second_db(pg):
    name = f"second_{secrets.token_hex(4)}"
    with pg.admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE pristine')
    yield f"postgresql://postgres:{pg.admin_pass}@127.0.0.1:{pg.port}/{name}"
    with pg.admin() as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_dump_then_restore_onto_a_blank_database_keeps_the_data(pg, blank_db, second_db, tmp_path):
    install(blank_db)
    out = tmp_path / "backup.sql.gz"
    result = run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size > 0 and oct(out.stat().st_mode & 0o777) == "0o600"
    result = run(pg, "restore", str(out), db=db_name(second_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert expenses(second_db) == [("Round trip shop", Decimal("321.50"))]
    with psycopg.connect(second_db) as conn:       # TeslaMate's own tables are left alone
        assert conn.execute("SELECT count(*) FROM cars").fetchone()[0] == 1


def test_the_dump_holds_only_the_garagebooks_schema_and_never_the_key(pg, blank_db, tmp_path):
    install(blank_db)
    out = tmp_path / "backup.sql"
    result = run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "CREATE TABLE garagebooks.expense" in text
    assert "CREATE TABLE public." not in text
    assert KEY not in text and KEY not in result.stdout + result.stderr


def test_dump_refuses_to_overwrite_an_existing_file(pg, blank_db, tmp_path):
    install(blank_db)
    out = tmp_path / "backup.sql"
    out.write_text("precious")
    result = run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode != 0 and "exists" in result.stderr
    assert out.read_text() == "precious"


def test_dump_without_a_file_name_writes_a_dated_file_in_the_current_folder(pg, blank_db, tmp_path):
    install(blank_db)
    result = run(pg, "dump", db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    files = list(tmp_path.glob("garagebooks-*.sql.gz"))
    assert len(files) == 1
    assert "CREATE TABLE garagebooks.expense" in gzip.open(files[0], "rt").read()


def test_dump_of_a_database_without_the_schema_fails_and_leaves_no_file(pg, blank_db, tmp_path):
    out = tmp_path / "backup.sql.gz"
    result = run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode != 0 and "no garagebooks schema" in result.stderr
    assert not out.exists()


def test_restore_refuses_to_overwrite_a_schema_that_has_data(pg, blank_db, second_db, tmp_path):
    install(blank_db, vendor="Original")
    out = tmp_path / "backup.sql"
    assert run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path).returncode == 0
    install(second_db, vendor="Newer, must survive")
    result = run(pg, "restore", str(out), db=db_name(second_db), cwd=tmp_path)
    assert result.returncode != 0 and "--replace" in result.stderr
    assert expenses(second_db) == [("Newer, must survive", Decimal("321.50"))]


def test_restore_with_replace_swaps_the_schema_for_the_backup(pg, blank_db, second_db, tmp_path):
    install(blank_db, vendor="Original")
    out = tmp_path / "backup.sql.gz"
    assert run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path).returncode == 0
    install(second_db, vendor="Newer, replaced")
    result = run(pg, "restore", "--replace", str(out), db=db_name(second_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert expenses(second_db) == [("Original", Decimal("321.50"))]


def test_a_failed_replace_leaves_the_existing_schema_alone(pg, blank_db, tmp_path):
    install(blank_db, vendor="Keep me")
    bad = tmp_path / "bad.sql"
    bad.write_text("CREATE SCHEMA garagebooks;\nCREATE TABLE garagebooks.x (;\n")
    result = run(pg, "restore", "--replace", str(bad), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode != 0
    assert expenses(blank_db) == [("Keep me", Decimal("321.50"))]


def test_restore_refuses_a_file_that_is_not_a_garagebooks_dump(pg, blank_db, tmp_path):
    other = tmp_path / "other.sql"
    other.write_text("CREATE TABLE public.cars (id int);\n")
    result = run(pg, "restore", str(other), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode != 0 and "garagebooks" in result.stderr


def test_restore_refuses_a_whole_database_backup(pg, blank_db, second_db, tmp_path):
    install(blank_db)
    whole = tmp_path / "whole.sql"
    whole.write_bytes(whole_database_dump(pg, blank_db))
    result = run(pg, "restore", str(whole), db=db_name(second_db), cwd=tmp_path)
    assert result.returncode != 0 and "whole" in result.stderr


def test_check_finds_the_schema_in_plain_and_gzip_backups(pg, blank_db, tmp_path):
    install(blank_db)
    dump = whole_database_dump(pg, blank_db)
    plain, packed = tmp_path / "tm.sql", tmp_path / "tm.sql.gz"
    plain.write_bytes(dump)
    packed.write_bytes(gzip.compress(dump))
    for path in (plain, packed):
        result = run(pg, "check", str(path), db=db_name(blank_db), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "includes the garagebooks schema" in result.stdout and "with its data" in result.stdout


def test_check_finds_the_schema_in_a_custom_format_backup(pg, blank_db, tmp_path):
    install(blank_db)
    path = tmp_path / "tm.dump"
    path.write_bytes(whole_database_dump(pg, blank_db, "-Fc"))
    result = run(pg, "check", str(path), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "includes the garagebooks schema" in result.stdout and "with its data" in result.stdout


def test_check_says_so_when_the_schema_is_missing(pg, blank_db, tmp_path):
    for name, data in (("old.sql", b"CREATE TABLE public.cars (id int);\n"),
                       ("old.sql.gz", gzip.compress(b"CREATE TABLE public.cars (id int);\n"))):
        path = tmp_path / name
        path.write_bytes(data)
        result = run(pg, "check", str(path), db=db_name(blank_db), cwd=tmp_path)
        assert result.returncode == 1
        assert "does not include the garagebooks schema" in result.stdout


def test_check_reports_a_structure_only_backup(pg, blank_db, tmp_path):
    path = tmp_path / "structure.sql"
    path.write_text("CREATE TABLE garagebooks.expense (id int);\n")
    result = run(pg, "check", str(path), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 1
    assert "structure only" in result.stdout


def test_check_rejects_a_missing_file(pg, blank_db, tmp_path):
    result = run(pg, "check", str(tmp_path / "nope.sql"), db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 2 and "not found" in result.stderr


def test_unknown_commands_print_usage(pg, blank_db, tmp_path):
    result = run(pg, "frobnicate", db=db_name(blank_db), cwd=tmp_path)
    assert result.returncode == 2 and "usage" in result.stderr.lower()


def test_the_script_does_not_read_the_encryption_key():
    assert "ENCRYPTION_KEY" not in SCRIPT.read_text()


def test_rows_that_look_like_sql_do_not_confuse_the_checks(pg, blank_db, second_db, tmp_path):
    install(blank_db, vendor="CREATE TABLE public.x")
    with psycopg.connect(blank_db, autocommit=True) as conn:
        conn.execute("UPDATE garagebooks.category SET name = E'\\\\evil' WHERE name = 'Tyres'")
    out = tmp_path / "backup.sql"
    assert run(pg, "dump", str(out), db=db_name(blank_db), cwd=tmp_path).returncode == 0
    result = run(pg, "restore", str(out), db=db_name(second_db), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert expenses(second_db) == [("CREATE TABLE public.x", Decimal("321.50"))]
