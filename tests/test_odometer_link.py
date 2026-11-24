"""The Add form's "Fill from TeslaMate" button is only for a car TeslaMate tracks, and only while TeslaMate answers."""
import re

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, sources

ORIGIN = {"Origin": "http://localhost"}


def app_with(pg, monkeypatch, state):
    monkeypatch.delenv("BASE_PATH", raising=False)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    monkeypatch.setattr(sources, "detect", lambda conn, allow_untested=False: sources.TeslaMate(state=state))
    app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row))
    app.testing = True
    return app.test_client()


def form_for(client, car_id):
    """The Add form with this car chosen (a post with nothing else is shown again with its errors)."""
    return client.post("/new", data={"car_id": str(car_id)}, headers=ORIGIN).data.decode()


@pytest.fixture()
def cars(admin):
    """Car 1 is linked to TeslaMate (id 1); car 2 is added by hand."""
    before = admin.execute("SELECT teslamate_car_id AS t FROM garagebooks.car WHERE id = 1").fetchone()["t"]
    admin.execute("UPDATE garagebooks.car SET teslamate_car_id = 1 WHERE id = 1")
    admin.execute("INSERT INTO garagebooks.car (id, name, kind) VALUES (2, 'By hand', 'BEV')")
    yield
    admin.execute("DELETE FROM garagebooks.car WHERE id = 2")
    admin.execute("UPDATE garagebooks.car SET teslamate_car_id = %s WHERE id = 1", (before,))   # the next test starts as it was


def option(html, car_id):
    return re.search(rf'<option value="{car_id}"[^>]*>', html).group(0)


def button(html):
    found = re.search(r'<button[^>]*id="fill-odometer"[^>]*>', html)
    return found.group(0) if found else None


def test_a_teslamate_car_gets_the_button_while_teslamate_answers(pg, admin, cars, monkeypatch):
    html = form_for(app_with(pg, monkeypatch, sources.OK), 1)
    assert "data-teslamate" in option(html, 1) and "data-teslamate" not in option(html, 2)
    assert button(html) and " hidden" not in button(html)


def test_a_car_added_by_hand_has_no_button_even_with_teslamate(pg, admin, cars, monkeypatch):
    html = form_for(app_with(pg, monkeypatch, sources.OK), 2)
    assert button(html) and " hidden" in button(html)       # there for a TeslaMate car chosen later, hidden now


@pytest.mark.parametrize("state", [sources.ABSENT, sources.UNAVAILABLE, sources.UNTESTED])
def test_no_button_when_teslamate_is_absent_or_held_back(pg, admin, cars, monkeypatch, state):
    html = form_for(app_with(pg, monkeypatch, state), 1)
    assert button(html) is None
    assert "data-teslamate" not in html
