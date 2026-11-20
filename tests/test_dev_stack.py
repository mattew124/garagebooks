"""The dev stack's one job is to stay away from live data. Guard that, the
schema dump filter, and the rules keeping the app behind a login."""

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV = (ROOT / "docker-compose.dev.yml").read_text()
DEV_PROXY = (ROOT / "docker-compose.dev.traefik.yml").read_text()
PROD_PROXY = (ROOT / "docker-compose.traefik.yml").read_text()
SCRIPT = (ROOT / "scripts" / "dev.sh").read_text()


def config():
    out = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.dev.yml"), "--env-file", "/dev/null",
         "config", "--format", "json", "--no-interpolate"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_dev_stack_only_has_its_own_network():
    cfg = config()
    assert set(cfg["networks"]) == {"dev"}
    assert "external" not in cfg["networks"]["dev"]
    assert "teslamate_default" not in DEV + DEV_PROXY


def test_test_database_is_never_published():
    db = config()["services"]["dev-db"]
    assert set(db["networks"]) == {"dev"} and "ports" not in db
    assert "dev-db:" not in DEV_PROXY


def test_host_ports_are_loopback_only():
    for name in ("garagebooks-dev", "dev-grafana"):
        (port,) = config()["services"][name]["ports"]
        assert port.startswith("127.0.0.1:")


def test_every_router_requires_a_forward_auth_middleware():
    for text in (DEV_PROXY, PROD_PROXY):
        routers = set(re.findall(r"routers\.(\S+?)\.rule=", text))
        assert routers
        for router in routers:
            assert re.search(rf"routers\.{re.escape(router)}\.middlewares=\$\{{AUTH_MIDDLEWARE:\?", text), router


def test_dev_routes_outrank_production():
    prod = int(re.search(r"garagebooks-secure\.priority=(\d+)", PROD_PROXY).group(1))
    dev = [int(p) for p in re.findall(r"\.priority=\$\{DEV_\w+_PRIORITY:-(\d+)\}", DEV_PROXY)]
    assert len(dev) == 2 and prod < min(dev)
    # Worktree priorities come from dev.sh; tests/test_worktrees.py checks their order.


def test_dev_routes_end_in_a_slash():
    """Otherwise /garagebooks-dev/foo would also serve /garagebooks-dev/foobar."""
    rules = re.findall(r"PathPrefix\(`([^`]+)`\)", DEV_PROXY)
    assert rules and all(r.endswith("/") for r in rules)


def test_only_reads_from_the_seed_source():
    sql = re.findall(r'source_psql "([^"]*)"', SCRIPT)
    assert sql
    for statement in sql:
        assert statement.startswith(("SELECT ", "COPY (SELECT ")), statement
        if statement.startswith("COPY"):
            assert statement.endswith("TO STDOUT"), statement
    # The only other command against the source is the schema-only dump, in its own script.
    execs = [l for l in SCRIPT.splitlines() if "docker exec" in l and "SEED_FROM" in l]
    assert len(execs) == 1
    dump = (ROOT / "scripts" / "dump-teslamate-schema.sh").read_text()
    (dump_exec,) = [l for l in dump.splitlines() if "docker exec" in l]
    assert "--schema-only" in dump_exec


def test_positions_are_hourly_and_stripped_of_location():
    for blanked in ("WHEN 'latitude'  THEN '0'", "WHEN 'longitude' THEN '0'", "WHEN 'elevation' THEN 'NULL'"):
        assert blanked in SCRIPT
    assert "date_trunc('hour', date)" in SCRIPT
    assert "location data reached the test database" in SCRIPT


def test_schema_filter():
    dump = r"""--
-- PostgreSQL database dump
--
\restrict abc123
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);

CREATE TABLE public.cars (
    id smallint NOT NULL
);


ALTER TABLE ONLY public.cars
    ADD CONSTRAINT cars_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cars
    ADD CONSTRAINT cars_settings_id_fkey FOREIGN KEY (settings_id) REFERENCES public.car_settings(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_car_id_fkey FOREIGN KEY (car_id) REFERENCES public.cars(id) ON DELETE CASCADE;
"""
    out = subprocess.run(["awk", "-f", str(ROOT / "scripts" / "filter-schema-dump.awk")],
                         input=dump, capture_output=True, text=True, check=True).stdout
    assert "restrict" not in out and "SET " not in out and "pg_catalog" not in out and "--" not in out
    assert "cars_pkey" in out and "positions_car_id_fkey" in out and "car_settings" not in out
    assert "ALTER TABLE ONLY public.cars\n    ADD CONSTRAINT cars_pkey" in out


def test_dashboard_has_no_hardcoded_timezone():
    dash = json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())
    assert any(v["name"] == "tz" for v in dash["templating"]["list"])
    sql = [t["rawSql"] for p in dash["panels"] for t in p["targets"]]
    assert any("AT TIME ZONE '$tz'" in s for s in sql)
    assert not any(re.search(r"AT TIME ZONE '[A-Z][a-z]+/", s) for s in sql)


def test_dashboard_expenses_link_comes_from_the_apps_own_settings():
    """A bare "/expenses/" resolves against Grafana's own host. The link is read from the
    URL saved on the app's Settings page instead."""
    dash = json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())
    (link,) = [l for l in dash["links"] if l.get("title") == "Add / edit expenses"]
    assert link["url"] == "${app_url}"
    var = next(v for v in dash["templating"]["list"] if v["name"] == "app_url")
    assert "garagebooks.setting" in var["query"]


def test_seeded_grafana_link_points_at_this_dashboards_uid():
    from garagebooks import db
    dash = json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())
    assert dash["uid"] == db.DASHBOARD_UID


def test_dashboard_units_never_auto_scale():
    """Grafana's km and currency units rescale: 57,197 km became "57 Mm", $1,359 became "$1K"."""
    raw = (ROOT / "grafana" / "cost-of-ownership.json").read_text()
    assert "lengthkm" not in raw and "currencyUSD" not in raw


def test_dev_script_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "dev.sh")], check=True)


def function_body(name):
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start:SCRIPT.index("\n}\n", start)]


def test_saved_onedrive_sign_in_never_includes_a_folder():
    """Worktrees share a saved sign-in, never the folder a stack is pointed at."""
    save, link = function_body("onedrive_save"), function_body("onedrive_link")
    assert "garagebooks.onedrive_account" in save and "car_folder" not in save
    assert "DELETE FROM garagebooks.car_folder" in link


def test_saved_onedrive_sign_in_is_private_and_outside_the_repo():
    save = function_body("onedrive_save")
    assert "umask 077" in save
    assert 'ONEDRIVE_DEV_FILE="$HOME/.config/' in SCRIPT
    assert "outside the repository" in function_body("onedrive_file_check")
    assert "onedrive_file_check" in save and "onedrive_file_check" in function_body("onedrive_link")


def test_onedrive_link_needs_no_other_stack():
    link = function_body("onedrive_link")
    assert "docker exec" not in link and "ONEDRIVE_SOURCE_DB" not in SCRIPT
    assert "--git-common-dir" not in link


def test_onedrive_link_keeps_the_saved_sign_in_fresh():
    """Microsoft replaces the refresh token when it's used, so the file gets the new one."""
    link = function_body("onedrive_link")
    assert link.index("onedrive-test-folder.py") < link.index("onedrive_save")


def test_real_uploads_are_kept_to_the_test_folder():
    browser = (ROOT / "tests" / "browser" / "run.py").read_text()
    assert 'TEST_FOLDER = "Expenses test"' in browser
    helper = (ROOT / "scripts" / "onedrive-test-folder.py").read_text()
    assert 'FOLDER = "Expenses test"' in helper



def test_sample_expenses_are_made_up_and_stay_in_the_test_database():
    """The sample data is for the dev stack only: it runs through dev_psql (the test database), writes
    nothing but expenses, reads only what the app reads, and holds nothing from a real installation."""
    sql = (ROOT / "scripts" / "dev-sample-expenses.sql").read_text()
    code = re.sub(r"--[^\n]*", "", sql)
    assert set(re.findall(r"(?:INSERT INTO|DELETE FROM|UPDATE)\s+([\w.]+)", code)) == {"garagebooks.expense"}
    assert set(re.findall(r"(?:FROM|JOIN)\s+((?:public|garagebooks)\.\w+)", code)) <= {
        "garagebooks.expense", "garagebooks.category", "public.cars", "public.positions"}
    assert set(re.findall(r"https?://([^/'\s]+)", sql)) == {"example.com"}
    assert 'dev_psql -v "tz=$APP_TZ" < scripts/dev-sample-expenses.sql' in SCRIPT
    assert "sample-expenses)\n        ensure_env" in SCRIPT      # never past the dev stack's own checks
    assert "LIKE 'Sample data%'" in sql and "LIKE 'Sample data%'" in SCRIPT   # clear removes only these


def test_what_an_installer_sees_is_named_garagebooks():
    """The product is GarageBooks: the shipped compose service, routers, default path, Grafana
    folder and dashboard links say so. Stored names (the expenses schema, EXPENSES_* settings,
    the dashboard uid) stay, because existing installs depend on them."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "\n  garagebooks:\n" in compose and "\n  expenses:\n" not in compose
    assert "BASE_PATH: ${BASE_PATH:-/garagebooks}" in compose
    assert "\n  garagebooks:\n" in PROD_PROXY and "tm-expenses" not in PROD_PROXY
    assert "PathPrefix(`${BASE_PATH:-/garagebooks}`)" in PROD_PROXY
    assert "folder: GarageBooks\n" in (ROOT / "grafana" / "provider.yml").read_text()
    dash = json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())
    (link,) = [l for l in dash["links"] if l.get("url") == "${app_url}"]
    assert link["tooltip"] == "Open GarageBooks"
    assert next(v for v in dash["templating"]["list"] if v["name"] == "app_url")["label"] == "GarageBooks URL"
    assert "/garagebooks/" in json.dumps(dash) and "'/expenses/'" not in json.dumps(dash)


def test_onedrive_sign_in_saved_before_the_rename_is_still_found():
    """onedrive-save wrote ~/.config/teslamate-expenses/onedrive-dev before the project was renamed."""
    out = subprocess.run(["bash", "-c", 'source <(sed -n "/^# ONEDRIVE_DEV_FILE start/,/^# ONEDRIVE_DEV_FILE end/p" scripts/dev.sh); echo "$ONEDRIVE_DEV_FILE"'],
                         cwd=ROOT, env={"HOME": "/h", "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "/h/.config/garagebooks/onedrive-dev"          # nothing saved anywhere: the new place
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        old = Path(home, ".config", "teslamate-expenses"); old.mkdir(parents=True); (old / "onedrive-dev").write_text("x")
        out = subprocess.run(["bash", "-c", 'source <(sed -n "/^# ONEDRIVE_DEV_FILE start/,/^# ONEDRIVE_DEV_FILE end/p" scripts/dev.sh); echo "$ONEDRIVE_DEV_FILE"'],
                             cwd=ROOT, env={"HOME": home, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True).stdout.strip()
        assert out == str(old / "onedrive-dev")


def compose_services(path):
    text = (ROOT / path).read_text()
    block = re.search(r"^services:\n((?:[ #].*\n|\n)*)", text, re.M)
    block = block.group(1) if block else ""
    return set(re.findall(r"^  ([a-z][\w-]*):\s*$", block, re.M))


def test_every_override_file_names_a_service_the_main_file_has():
    """An override for a service that doesn't exist starts a broken second container instead."""
    main = compose_services("docker-compose.yml")
    assert main == {"garagebooks", "worker"}
    for override in ("docker-compose.traefik.yml", "docker-compose.invoices.yml"):
        assert compose_services(override) and compose_services(override) <= main, override


def test_the_stand_in_claims_connector_is_private_and_the_app_uses_it_by_default():
    """Claiming is on in the dev stack against tests/fake_connector.py, which is never published and only
    ever answers with invented values; DEV_CLAIMS_CONNECTOR_URL points the app at a real one instead."""
    cfg = config()
    fake = cfg["services"]["dev-claims-connector"]
    assert set(fake["networks"]) == {"dev"} and "ports" not in fake
    assert "dev-claims-connector:" not in DEV_PROXY
    env = cfg["services"]["garagebooks-dev"]["environment"]
    assert "dev-claims-connector:8765" in env["CLAIMS_CONNECTOR_URL"]
    assert "DEV_CLAIMS_CONNECTOR_URL" in env["CLAIMS_CONNECTOR_URL"]
    # signed tokens: the app gets a private key, the stand-in only the public half. No shared token any more.
    assert env["CLAIMS_CONNECTOR_KEY_FILE"] == "/run/claims/connector.key"
    assert "CLAIMS_CONNECTOR_TOKEN" not in env and "DEV_CLAIMS_CONNECTOR_TOKEN" not in DEV
    assert fake["environment"]["FAKE_CONNECTOR_KEYS_DIR"] == "/claims-keys"
    app_mounts = {v["target"]: v for v in cfg["services"]["garagebooks-dev"]["volumes"]}
    assert app_mounts["/run/claims/connector.key"]["read_only"] is True
    assert not app_mounts["/run/claims/connector.key"].get("bind", {}).get("create_host_path", True)
    fake_mounts = {v["target"]: v for v in fake["volumes"]}
    assert "stand-in.key" not in fake_mounts["/claims-keys"]["source"] and fake_mounts["/claims-keys"]["read_only"]


def test_the_stand_in_gets_a_fresh_key_pair_on_each_start_and_only_the_public_half():
    assert "openssl genpkey -algorithm ed25519" in SCRIPT and "openssl pkey" in SCRIPT and "-pubout" in SCRIPT
    start = SCRIPT[SCRIPT.index("\nstart() {"):]
    assert start.index("claims_keys") < start.index("compose up")


# ---- a real claims connector in dev.env ----------------------------------------------------------------------

def run_dev(tmp_path, dev_env, *args, env=None):
    """dev.sh from a scratch copy of the checkout, with this dev.env: never touches docker or the real one."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "dev.sh").write_text(SCRIPT)
    (tmp_path / "dev.env").write_text(dev_env)
    return subprocess.run(["bash", str(tmp_path / "scripts" / "dev.sh"), *args], cwd=tmp_path, capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(env or {})})


def test_a_real_connector_without_its_token_stops_the_stack_starting(tmp_path):
    done = run_dev(tmp_path, 'DEV_CLAIMS_CONNECTOR_URL=http://connector.invalid:8765\n', "up")
    assert done.returncode == 2 and "DEV_CLAIMS_CONNECTOR_KEY_FILE is empty" in done.stderr


def test_a_real_connector_with_an_unreadable_key_file_stops_the_stack_starting(tmp_path):
    done = run_dev(tmp_path, 'DEV_CLAIMS_CONNECTOR_URL=http://connector.invalid:8765\n'
                             f'DEV_CLAIMS_CONNECTOR_KEY_FILE={tmp_path}/nope.key\n', "up")
    assert done.returncode == 2 and "can't read" in done.stderr and "nope.key" in done.stderr


def test_the_browser_tests_refuse_to_run_against_a_real_connector(tmp_path):
    done = run_dev(tmp_path, 'DEV_CLAIMS_CONNECTOR_URL=http://connector.invalid:8765\nDEV_CLAIMS_CONNECTOR_KEY_FILE=x\n',
                   "test-browser")
    assert done.returncode == 2 and "stand-in" in done.stderr and "STAND_IN=1" in done.stderr


def test_stand_in_wins_over_dev_env():
    assert 'if [ "${STAND_IN:-}" = 1 ]; then unset DEV_CLAIMS_CONNECTOR_URL DEV_CLAIMS_CONNECTOR_KEY_FILE; fi' in SCRIPT
    assert SCRIPT.index("STAND_IN:-") > SCRIPT.index(". ./dev.env")           # applied after dev.env is read


def merged_compose(tmp_path, *files, profiles=(), env=None):
    """The compose file as Docker reads it: anchors, profiles and overrides resolved. -> the services."""
    for name in {"docker-compose.yml", *files}:
        (tmp_path / name).write_text((ROOT / name).read_text())
    (tmp_path / ".env").write_text("")
    cmd = ["docker", "compose", *sum((["-f", f] for f in ("docker-compose.yml", *files)), []),
           *sum((["--profile", p] for p in profiles), []), "config", "--format", "json"]
    out = subprocess.run(cmd, cwd=tmp_path, env={**{k: v for k, v in os.environ.items() if k != "SCHEDULER"}, **(env or {})}, capture_output=True, text=True, check=True).stdout
    return json.loads(out)["services"]


def test_the_worker_is_off_unless_its_profile_is_asked_for(tmp_path):
    assert set(merged_compose(tmp_path)) == {"garagebooks"}
    assert set(merged_compose(tmp_path, profiles=["worker"])) == {"garagebooks", "worker"}


def test_the_worker_is_the_same_image_as_hard_as_the_app_and_runs_the_jobs_only(tmp_path):
    services = merged_compose(tmp_path, profiles=["worker"])
    app, worker = services["garagebooks"], services["worker"]
    assert worker["image"] == app["image"] and worker["command"] == ["python3", "-m", "garagebooks", "worker"]
    for key in ("read_only", "cap_drop", "security_opt", "tmpfs", "restart", "networks", "volumes"):
        assert worker[key] == app[key], key
    assert worker["read_only"] is True and worker["cap_drop"] == ["ALL"] and "no-new-privileges:true" in worker["security_opt"]
    assert "ports" not in worker and worker["healthcheck"] == {"disable": True}      # it listens on nothing
    assert worker["environment"]["SCHEDULER"] == "worker"
    assert {k: v for k, v in worker["environment"].items() if k != "SCHEDULER"} == \
        {k: v for k, v in app["environment"].items() if k != "SCHEDULER"}
    assert any(v["target"] == "/dashboards" for v in worker["volumes"])               # the dashboard job writes there


def test_the_app_runs_the_jobs_itself_unless_told_otherwise(tmp_path):
    assert merged_compose(tmp_path)["garagebooks"]["environment"]["SCHEDULER"] == "app"
    assert merged_compose(tmp_path, env={"SCHEDULER": "worker"})["garagebooks"]["environment"]["SCHEDULER"] == "worker"


def test_the_worker_gets_the_invoice_folder_the_app_has(tmp_path):
    services = merged_compose(tmp_path, "docker-compose.invoices.yml", profiles=["worker"], env={"INVOICES_HOST_DIR": "/srv/invoices"})
    app, worker = services["garagebooks"], services["worker"]
    assert worker["user"] == app["user"] and worker["volumes"] == app["volumes"]
    assert worker["environment"]["INVOICES_DIR"] == "/invoices" and worker["environment"]["SCHEDULER"] == "worker"
    assert worker["command"] == ["python3", "-m", "garagebooks", "worker"]


def test_the_dev_stack_can_run_the_worker_but_only_when_asked():
    """SCHEDULER=worker in dev.env turns the profile on; anything else leaves dev on the app's own jobs."""
    assert 'profiles: ["worker"]' in DEV and 'command: ["python3", "-m", "garagebooks", "worker"]' in DEV
    assert "SCHEDULER: ${SCHEDULER:-app}" in DEV and "SCHEDULER: worker" in DEV
    assert 'COMPOSE_PROFILES="$profile"' in SCRIPT and '!= worker ] && [ "${1:-}" != down' in SCRIPT   # down stops it too
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
    out = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.dev.yml"), "--env-file", "/dev/null",
                          "config", "--services"], capture_output=True, text=True, check=True, env=env).stdout.split()
    assert "dev-worker" not in out and "garagebooks-dev" in out


def test_the_browser_tests_python_package_matches_their_playwright_image():
    """The image ships the browsers but not the Python package, which looks for its own build of them.
    The package's version is in tests/browser/requirements.txt, where Dependabot's pip updates find it."""
    image = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-",
                      (ROOT / "tests" / "browser" / "Dockerfile").read_text(), re.M)
    package = re.search(r"^playwright==(\d+\.\d+\.\d+)$",
                        (ROOT / "tests" / "browser" / "requirements.txt").read_text(), re.M)
    assert image and package, "the browser tests' image or package version can't be found"
    assert package.group(1) == image.group(1)


def test_dependabot_bumps_the_browser_tests_image_and_package_in_one_pull_request():
    """Bumped apart, the two would never match: one group holds both, and nothing else."""
    import yaml

    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    browser = [u for u in config["updates"] if "/tests/browser" in (u.get("directories") or [u.get("directory")])]
    assert sorted(u["package-ecosystem"] for u in browser) == ["docker", "pip"]
    assert {u.get("multi-ecosystem-group") for u in browser} == {"playwright"}
    assert all(u.get("patterns") for u in browser)
    assert "schedule" in config["multi-ecosystem-groups"]["playwright"]
    others = [u for u in config["updates"] if u not in browser]
    assert all("/tests/browser" not in (u.get("directories") or [u.get("directory")]) for u in others)


def test_onedrive_link_signs_in_at_the_microsoft_address():
    """The registration's redirect URI is /microsoft/callback; a linked stack must not ask for the older one."""
    assert "callback = 'microsoft'" in function_body("onedrive_link")
