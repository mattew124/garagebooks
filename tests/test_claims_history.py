"""The Claims page as a ledger: every expense with where it stands, and the provider's transaction history,
cached in the database and lined up with the expenses. The connector is tests/fake_connector.py; all values
are invented. Runs on either kind of database."""

import re

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.test_claims import (  # noqa: F401 - fixtures used by name
    ORIGIN, category_id, client, expense, fake, lease, make_client, said, submit, tyres)


def tx(pid="T1", day="2026-11-07", amount=1200.0, kind="claim_payment", car_id=1, claim=None, **extra):
    return {"provider_id": pid, "date": day, "amount": amount, "gst": round(amount / 11, 2), "type": "Reimbursement",
            "status": "Paid", "description": "Tyres", "payee": "Example Tyres Pty Ltd", "kind": kind,
            "car_id": car_id, "claim_provider_id": claim, "reference": None, **extra}


def sync(client, fake, *rows):
    fake.config["transactions"] = list(rows)
    resp = client.post("/claims/sync", headers=ORIGIN)
    assert resp.status_code == 303, resp.data
    return resp


def stored(admin):
    return admin.execute("SELECT * FROM garagebooks.provider_transaction ORDER BY provider_id").fetchall()


def row_of(page, attr, value):
    """The <tr> carrying data-<attr>="<value>", as text (empty when absent)."""
    found = re.search(rf'<tr[^>]*data-{attr}="{value}"[^>]*>.*?</tr>', page, re.S)
    return found.group(0) if found else ""


def second_car(admin):
    admin.execute("INSERT INTO garagebooks.car (id, name, kind, make, model) VALUES (2, 'Second car', 'ICE', 'Example', 'Model B')")
    lease(admin, car=2)


# ---- off when there is no connector --------------------------------------------------------------------

def test_without_a_connector_there_is_no_sync(pg, monkeypatch, tmp_path):
    client = make_client(pg, monkeypatch, tmp_path, None)
    assert client.post("/claims/sync", headers=ORIGIN).status_code == 404


# ---- the cache ------------------------------------------------------------------------------------------

def test_a_sync_keeps_the_providers_transactions_and_does_not_repeat_them(client, admin, fake):
    sync(client, fake, tx("T1"), tx("T2", amount=62.1, day="2026-11-09"))
    sync(client, fake, tx("T1"), tx("T2", amount=62.1, day="2026-11-09", status="Processed"))
    rows = stored(admin)
    assert [r["provider_id"] for r in rows] == ["T1", "T2"] and rows[1]["status"] == "Processed"
    assert str(rows[1]["amount"]) == "62.10" and rows[0]["synced_at"] is not None
    assert last_call(fake, "/v1/transactions")[3]["cars"][0]["id"] == 1        # the connector is told the cars


def claims_made(fake):
    return [c for c in fake.calls if c[1] in ("/v1/preview", "/v1/submit")]


def last_call(fake, path):
    return [c for c in fake.calls if c[1] == path][-1]


def test_junk_rows_are_skipped_not_fatal(client, admin, fake):
    sync(client, fake, tx("T1"), {"provider_id": "", "date": "2026-01-01", "amount": 5}, {"provider_id": "T3", "date": "nope", "amount": 5},
         {"provider_id": "T4", "date": "2026-01-01", "amount": "x"}, "text", tx("T5", kind="weird", car_id=99))
    rows = {r["provider_id"]: r for r in stored(admin)}
    assert set(rows) == {"T1", "T5"} and rows["T5"]["kind"] == "other" and rows["T5"]["car_id"] is None


def test_the_page_opens_from_the_cache_when_the_connector_is_down(client, admin, fake):
    sync(client, fake, tx("T1"))
    fake.stop()
    page = said(client.get("/claims"))
    assert "Example Tyres Pty Ltd" in page and "can't be reached" in page


def test_a_sync_when_signed_out_offers_the_sign_in(client, admin, fake):
    fake.config["signed_in"] = False
    resp = client.post("/claims/sync", headers=ORIGIN)
    page = said(client.get(resp.headers["Location"]))
    assert 'id="claim-signin"' in page and stored(admin) == []


def test_a_connector_error_on_sync_is_shown_and_keeps_the_cache(client, admin, fake):
    sync(client, fake, tx("T1"))
    fake.config["override"] = {"transactions": (502, {"ok": False, "error": "The provider's site is down."})}
    page = said(client.get(client.post("/claims/sync", headers=ORIGIN).headers["Location"]))
    assert "site is down" in page and len(stored(admin)) == 1


# ---- lining transactions up with expenses ---------------------------------------------------------------

def test_a_transaction_follows_the_claim_made_from_the_app(client, admin, fake, tyres):
    submit(client, tyres)                                         # the fake answers provider id P0001
    sync(client, fake, tx("T1", amount=1199.0, day="2026-11-12", claim="P0001"))   # a different date and amount
    assert stored(admin)[0]["expense_id"] == tyres
    page = said(client.get("/claims"))
    assert f'/{tyres}/edit' in row_of(page, "tx", "T1")


def test_a_payment_matches_one_expense_by_car_date_and_amount(client, admin, fake):
    lease(admin)
    a = expense(admin, amount="1200.00")
    b = expense(admin, amount="80.00", at="2026-11-08 04:05+00")
    sync(client, fake, tx("T1"), tx("T2", amount=80.0, day="2026-11-08"), tx("T3", amount=77.0, day="2026-11-08"))
    got = {r["provider_id"]: r["expense_id"] for r in stored(admin)}
    assert got == {"T1": a, "T2": b, "T3": None}


def test_a_payment_that_fits_two_expenses_is_left_unmatched(client, admin, fake):
    lease(admin)
    expense(admin, amount="80.00")
    expense(admin, amount="80.00")
    sync(client, fake, tx("T1", amount=80.0))
    assert stored(admin)[0]["expense_id"] is None


def test_another_cars_expense_is_never_a_match(client, admin, fake):
    second_car(admin)
    expense(admin, car=2)
    sync(client, fake, tx("T1", car_id=1))
    assert stored(admin)[0]["expense_id"] is None


def test_only_claim_payments_match_by_amount(client, admin, fake):
    lease(admin)
    expense(admin)
    sync(client, fake, tx("T1", kind="other"))
    assert stored(admin)[0]["expense_id"] is None


def test_per_km_charging_needs_no_expense_and_is_not_flagged(client, admin, fake):
    lease(admin)
    sync(client, fake, tx("K1", kind="per_km_charging", amount=42.0), tx("T9", amount=5.0))
    page = said(client.get("/claims"))
    assert row_of(page, "match", "per-km") and "no expense needed" in row_of(page, "match", "per-km").lower()
    assert "No matching expense" in row_of(page, "tx", "T9")
    assert "No matching expense" not in row_of(page, "tx", "K1")
    only = said(client.get("/claims?tx=unmatched"))
    assert row_of(only, "tx", "T9") and not row_of(only, "tx", "K1")


def test_rematching_follows_changes_and_a_deleted_expense_frees_the_transaction(client, admin, fake):
    lease(admin)
    a = expense(admin)
    sync(client, fake, tx("T1"))
    assert stored(admin)[0]["expense_id"] == a
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (a,))
    assert stored(admin)[0]["expense_id"] is None


def test_an_expense_paid_outside_the_app_is_not_offered_for_claiming_again(client, admin, fake, tyres):
    sync(client, fake, tx("T1"))                                   # matched by car, date and amount
    page = said(client.get("/claims"))
    assert row_of(page, "expense", str(tyres)).count("data-state=\"matched\"") == 1
    assert f"/{tyres}/claim" not in page
    assert "already" in said(client.get(f"/{tyres}/claim")).lower() and not claims_made(fake)


def test_removing_a_claim_record_frees_its_transaction_so_the_expense_can_be_claimed_again(client, admin, fake, tyres):
    submit(client, tyres)
    sync(client, fake, tx("T1", amount=1199.0, claim="P0001"))
    assert stored(admin)[0]["expense_id"] == tyres
    client.post(f"/{tyres}/claim/forget", headers=ORIGIN)
    assert stored(admin)[0]["expense_id"] is None
    assert f"/{tyres}/claim" in said(client.get("/claims"))


# ---- every expense is on the page, with where it stands -------------------------------------------------

def test_every_expense_is_listed_with_its_state_or_the_reason_it_cant_be_claimed(client, admin, tyres):
    claimed = expense(admin, category="Insurance", amount="900.00", at="2026-10-01 04:00+00")
    submit(client, claimed)
    nopool = expense(admin, category="Initial purchase", amount="50000.00", at="2026-02-01 04:00+00")
    refund = expense(admin, amount="-100.00", at="2026-11-02 04:00+00")
    nolease = expense(admin, amount="10.00", at="2020-01-01 04:00+00")
    page = said(client.get("/claims"))
    assert 'data-state="ready"' in row_of(page, "expense", str(tyres)) and f"/{tyres}/claim" in page
    assert "REF0001" in row_of(page, "expense", str(claimed)) and 'data-state="claimed"' in row_of(page, "expense", str(claimed))
    for eid, why in ((nopool, "budget pool"), (refund, "refund"), (nolease, "no lease")):
        row = row_of(page, "expense", str(eid))
        assert 'data-state="blocked"' in row and why in row.lower(), (eid, row)
        assert f"/{eid}/claim" not in row


# ---- per car and other filters --------------------------------------------------------------------------

def test_the_claims_page_filters_by_car(client, admin, fake, tyres):
    second_car(admin)
    other = expense(admin, car=2, amount="300.00")
    sync(client, fake, tx("T1"), tx("T2", amount=300.0, car_id=2))
    page = said(client.get("/claims?car=2"))
    assert row_of(page, "expense", str(other)) and not row_of(page, "expense", str(tyres))
    assert row_of(page, "tx", "T2") and not row_of(page, "tx", "T1")
    both = said(client.get("/claims"))
    assert row_of(both, "expense", str(tyres)) and row_of(both, "expense", str(other))
    assert 'name="car"' in both and "Second car" in both
    assert client.get("/claims?car=abc").status_code == 200       # junk is ignored, not an error


def test_transactions_filter_by_date_and_the_expenses_by_state(client, admin, fake, tyres):
    claimed = expense(admin, category="Insurance", amount="900.00", at="2026-10-01 04:00+00")
    submit(client, claimed)
    sync(client, fake, tx("T1", day="2026-09-01"), tx("T2", day="2026-11-09", amount=5.0))
    page = said(client.get("/claims?from=2026-10-15&to=2026-12-31"))
    assert row_of(page, "tx", "T2") and not row_of(page, "tx", "T1")
    assert row_of(page, "expense", str(tyres)) and not row_of(page, "expense", str(claimed))   # the dates filter both tables
    ready = said(client.get("/claims?state=ready"))
    assert row_of(ready, "expense", str(tyres)) and not row_of(ready, "expense", str(claimed))
    assert client.get("/claims?from=x&to=y&state=zzz&tx=zzz").status_code == 200


# ---- the provider's sign-in status on the page ----------------------------------------------------------

def test_the_page_says_whether_the_provider_is_signed_in(client, admin, fake):
    page = said(client.get("/claims"))
    assert "Example Salary Packaging" in page and "Signed in" in page and 'id="claim-signin"' not in page
    fake.config["signed_in"] = False
    page = said(client.get("/claims"))
    assert "Not signed in" in page and 'id="claim-signin"' in page and "Sign in" in page
    assert 'data-retry-url' in page


def test_the_page_says_when_the_connector_cant_be_reached(client, admin, fake):
    fake.stop()
    page = said(client.get("/claims"))
    assert "can't be reached" in page and 'id="claim-signin"' not in page


# ---- storage --------------------------------------------------------------------------------------------

def test_the_restricted_role_can_use_the_cache(pg, admin):
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as app:
        app.execute("INSERT INTO garagebooks.provider_transaction (provider, provider_id, occurred_on, amount)"
                    " VALUES ('P', 'x', '2026-01-01', 1)")
        app.execute("UPDATE garagebooks.provider_transaction SET status = 'Paid'")
        assert app.execute("DELETE FROM garagebooks.provider_transaction").rowcount == 1


def test_a_transaction_is_unique_per_provider_and_key(admin):
    q = "INSERT INTO garagebooks.provider_transaction (provider, provider_id, occurred_on, amount) VALUES ('P', 'x', '2026-01-01', 1)"
    admin.execute(q)
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(q)
    admin.execute("DELETE FROM garagebooks.provider_transaction")
