"""Fixtures: a throwaway Postgres container that the real TeslaMate image (the release
compat.py says this app was tested with) migrates, so the whole suite runs on TeslaMate's own
tables, which are pulled for the test and not kept in this repository. The database is then
provisioned by the real scripts/provision-db.sh, so the tests also cover the schema, the grants
and the provisioning script.

Set TEST_TESLAMATE=0 to run the suite on plain PostgreSQL instead, with no TeslaMate tables at all:
GarageBooks must work both ways, and CI runs both. Tests that need TeslaMate's tables are marked
`@pytest.mark.teslamate` and skipped there; `no_teslamate` marks the ones only for the plain run.

Set TEST_PG_IMAGE to use another Postgres image, TEST_TESLAMATE_IMAGE (e.g. teslamate/teslamate:4.5.0)
to try another TeslaMate release (scripts/teslamate-compat.sh does this). Skipped when docker isn't
available.
"""

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import psycopg
import pytest
from hypothesis import settings
from psycopg.rows import dict_row

from garagebooks import compat

# Property-based tests (tests/test_properties*.py). `default` keeps a local run quick; CI sets
# HYPOTHESIS_PROFILE=ci for more examples. deadline=None everywhere: some properties go through
# the throwaway Postgres, and a slow example is not a failing one.
settings.register_profile("default", max_examples=50, deadline=None)
settings.register_profile("ci", max_examples=200, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

ROOT =Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("TEST_PG_IMAGE", "postgres:18-trixie")

TESLAMATE = os.environ.get("TEST_TESLAMATE", "1").strip() != "0"
TESLAMATE_IMAGE = os.environ.get("TEST_TESLAMATE_IMAGE") or f"teslamate/teslamate:{compat.TESTED_TESLAMATE}"
CAR = """
INSERT INTO cars (id, eid, vid, vin, settings_id, name, inserted_at, updated_at)
VALUES (1, 1, 1, 'TESTVIN', 1, 'Test car', now(), now());
"""


# The same car when there is no TeslaMate: one added by hand, with the id the tests use.
OWN_CAR = "INSERT INTO garagebooks.car (id, name, kind) VALUES (1, 'Test car', 'BEV') ON CONFLICT DO NOTHING;"


def pytest_configure(config):
    config.addinivalue_line("markers", "teslamate: needs TeslaMate's tables (skipped when TEST_TESLAMATE=0)")
    config.addinivalue_line("markers", "no_teslamate: only for a database with no TeslaMate (TEST_TESLAMATE=0)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("teslamate") and not TESLAMATE:
            item.add_marker(pytest.mark.skip(reason="needs TeslaMate's tables; this run has none (TEST_TESLAMATE=0)"))
        if item.get_closest_marker("no_teslamate") and TESLAMATE:
            item.add_marker(pytest.mark.skip(reason="only for a database with no TeslaMate (TEST_TESLAMATE=0)"))


def sh(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


class Pg:
    def __init__(self, container, port, app_pass):
        self.container, self.port, self.app_pass = container, port, app_pass
        self.app_url = f"postgresql://garagebooks_app:{app_pass}@127.0.0.1:{port}/teslamate"

    def psql(self, sql):
        return sh("docker", "exec", "-i", self.container, "psql", "-X", "-q", "-v",
                  "ON_ERROR_STOP=1", "-U", "postgres", "-d", "teslamate", "-c", sql)

    def provision(self):
        env = {**os.environ, "DB_CONTAINER": self.container, "GARAGEBOOKS_DB_PASS": self.app_pass}
        return subprocess.run([str(ROOT / "scripts" / "provision-db.sh")], env=env,
                              capture_output=True, text=True)

    def admin(self):
        # Superuser over the container's local socket is only reachable via exec;
        # for the host we use the postgres password set at container start.
        return psycopg.connect(
            f"postgresql://postgres:{self.admin_pass}@127.0.0.1:{self.port}/teslamate",
            row_factory=dict_row, autocommit=True)


@pytest.fixture(scope="session")
def pg():
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    name = f"garagebooks-test-{secrets.token_hex(4)}"
    admin_pass = secrets.token_hex(16)
    try:
        sh("docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432",
           "-e", f"POSTGRES_PASSWORD={admin_pass}", "-e", "POSTGRES_DB=teslamate", IMAGE)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"cannot start {IMAGE}: {exc.stderr.strip()}")
    try:
        port = sh("docker", "port", name, "5432/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        pg = Pg(name, port, secrets.token_hex(16))
        pg.admin_pass = admin_pass
        for _ in range(60):  # initdb restarts the server once; wait for the second start
            try:
                pg.admin().close()
                time.sleep(1)
                pg.admin().close()
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            pytest.fail("test Postgres never became ready")
        if TESLAMATE:
            # The image's entrypoint applies TeslaMate's migrations, then runs the no-op command.
            sh("docker", "run", "--rm", "--network", f"container:{name}", "-e", "DATABASE_HOST=127.0.0.1",
               "-e", "DATABASE_USER=postgres", "-e", f"DATABASE_PASS={admin_pass}", "-e", "DATABASE_NAME=teslamate",
               "-e", "ENCRYPTION_KEY=test-only", "-e", "DISABLE_MQTT=true", TESLAMATE_IMAGE, "/bin/dash", "-c", "true")
            pg.psql("INSERT INTO car_settings (id) VALUES (1), (2) ON CONFLICT DO NOTHING;" + CAR)
        # A copy of the database as TeslaMate leaves it, for blank_db: made from another database,
        # because a template can't be in use while it is copied.
        sh("docker", "exec", name, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres",
           "-c", "CREATE DATABASE pristine TEMPLATE teslamate")
        result = pg.provision()
        assert result.returncode == 0, result.stderr
        if not TESLAMATE:
            pg.psql(OWN_CAR)
        yield pg
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture()
def admin(pg):
    """Superuser connection with a clean slate of expenses + TeslaMate stub data."""
    with pg.admin() as conn:
        conn.execute("TRUNCATE garagebooks.expense, garagebooks.car_folder, garagebooks.onedrive_account,"
                     " garagebooks.learned_label, garagebooks.learned_vendor, garagebooks.import_skip,"
                     " garagebooks.odometer_reading, garagebooks.reminder RESTART IDENTITY CASCADE")
        if TESLAMATE:
            conn.execute("TRUNCATE positions, charging_processes RESTART IDENTITY CASCADE")   # CASCADE: real TeslaMate tables (drives...) reference them
        conn.execute("UPDATE garagebooks.provider_sync SET state = 'idle', started_at = NULL, finished_at = NULL,"
                     " outcome = NULL, message = NULL, since = NULL")      # no sync from an earlier test
        conn.execute("DELETE FROM garagebooks.lease")          # a test's lease on the first car; its budgets go with it
        conn.execute("DELETE FROM garagebooks.car WHERE id <> 1")
        if not TESLAMATE:
            conn.execute(OWN_CAR)       # a test may have deleted it
        conn.execute("UPDATE garagebooks.car SET name = 'Test car', kind = 'BEV', name_from_teslamate = %s WHERE id = 1",
                     (TESLAMATE,))
        conn.execute("SELECT garagebooks.build_ownership(true)")
        conn.execute("DELETE FROM garagebooks.category WHERE sort_order = 100")
        # The history is append-only, even for us: lift the guard just to start each test empty.
        conn.execute("ALTER TABLE garagebooks.change_log DISABLE TRIGGER USER;"
                     " TRUNCATE garagebooks.change_log RESTART IDENTITY;"
                     " ALTER TABLE garagebooks.change_log ENABLE TRIGGER USER")
        conn.execute("UPDATE garagebooks.setting SET invoice_date_format = NULL, invoice_name_format = NULL, multi_amount = NULL, lease_budgets = false")
        yield conn


@pytest.fixture()
def blank_db(pg):
    """A database with no `garagebooks` schema: as TeslaMate leaves it, or (TEST_TESLAMATE=0) empty."""
    name = f"blank_{secrets.token_hex(4)}"
    with pg.admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE pristine')
    url = f"postgresql://postgres:{pg.admin_pass}@127.0.0.1:{pg.port}/{name}"
    yield url
    with pg.admin() as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture()
def empty_db(pg):
    """A PostgreSQL database with nothing in it: no TeslaMate, no `garagebooks` schema. Works in
    either kind of run, so "no TeslaMate" is tested every time, not only with TEST_TESLAMATE=0."""
    name = f"empty_{secrets.token_hex(4)}"
    with pg.admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    url = f"postgresql://postgres:{pg.admin_pass}@127.0.0.1:{pg.port}/{name}"
    yield url
    with pg.admin() as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
