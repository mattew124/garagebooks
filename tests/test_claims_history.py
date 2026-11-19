"""The Claims page as a ledger: the All page's expense list with where each expense stands on claiming, and the
provider's claim and transaction history, cached in the database. Payments are tied to claims (which may be paid
in parts), and claims to expenses. The connector is tests/fake_connector.py; all values
are invented. Runs on either kind of database."""

import re
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.test_claims import (  # noqa: F401 - fixtures used by name
    ORIGIN, category_id, client, expense, fake, lease, make_client, said, submit, tyres)


def tx(pid="T1", day="2026-11-07", amount=1200.0, kind="claim_payment", car_id=1, claim=None, **extra):
    return {"provider_id": pid, "date": day, "amount": amount, "gst": round(amount / 11, 2), "type": "Reimbursement",
            "status": "Paid", "description": "Tyres", "payee": "Example Tyres Pty Ltd", "kind": kind,
            "car_id": car_id, "claim_provider_id": claim, "reference": None, **extra}


def line(claim="P0001", line_id="L1", day="2026-11-07", amount=1200.0, approved=None, car_id=1, reference="REF0001", **extra):
    return {"provider_id": claim, "reference": reference, "line_id": line_id, "date": day, "amount": amount,
            "approved_amount": approved, "status": "Approved", "paid_date": None, "description": "Tyres",
            "car_id": car_id, **extra}


def sync(client, fake, *rows, claims=()):
    fake.config["transactions"] = list(rows)
    fake.config["claims"] = list(claims)
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

def last_call(fake, path):
    return [c for c in fake.calls if c[1] == path][-1]


def claims_made(fake):
    return [c for c in fake.calls if c[1] in ("/v1/preview", "/v1/submit")]


def lines(admin):
    return admin.execute("SELECT * FROM garagebooks.provider_claim ORDER BY provider_id, line_id").fetchall()


def test_a_sync_keeps_the_providers_history_under_its_name_and_does_not_repeat_it(client, admin, fake):
    sync(client, fake, tx("T1"), tx("T2", amount=62.1, day="2026-11-09"), claims=[line()])
    sync(client, fake, tx("T1"), tx("T2", amount=62.1, day="2026-11-09", status="Processed"), claims=[line(approved=1100.0)])
    rows = stored(admin)
    assert [r["provider_id"] for r in rows] == ["T1", "T2"] and rows[1]["status"] == "Processed"
    assert str(rows[1]["amount"]) == "62.10" and rows[0]["provider"] == "Example Salary Packaging"
    (claim,) = lines(admin)
    assert claim["approved_amount"] == Decimal("1100.00") and claim["provider"] == "Example Salary Packaging"
    assert last_call(fake, "/v1/transactions")[3]["cars"][0]["id"] == 1        # the connector is told the cars


def test_a_connector_that_starts_naming_itself_does_not_duplicate_the_cache(client, admin, fake):
    admin.execute("INSERT INTO garagebooks.provider_transaction (provider, provider_id, occurred_on, amount)"
                  " VALUES ('Claims connector', 'T1', '2026-11-07', 1200)")
    admin.execute("INSERT INTO garagebooks.provider_claim (provider, provider_id, occurred_on, amount)"
                  " VALUES ('Claims connector', 'P0001', '2026-11-07', 1200)")
    sync(client, fake, tx("T1"), claims=[line()])
    assert [r["provider"] for r in stored(admin)] == ["Example Salary Packaging"]
    assert [r["provider"] for r in lines(admin)] == ["Example Salary Packaging"]


def test_a_sync_drops_what_the_provider_no_longer_reports(client, admin, fake):
    sync(client, fake, tx("T1"), tx("T2", amount=5.0), claims=[line(), line("P2", line_id="L2")])
    sync(client, fake, tx("T2", amount=5.0), claims=[line()])
    assert [r["provider_id"] for r in stored(admin)] == ["T2"] and [c["provider_id"] for c in lines(admin)] == ["P0001"]
    sync(client, fake)
    assert stored(admin) == [] and lines(admin) == []


def test_junk_rows_are_skipped_not_fatal(client, admin, fake):
    sync(client, fake, tx("T1"), {"provider_id": "", "date": "2026-01-01", "amount": 5}, {"provider_id": "T3", "date": "nope", "amount": 5},
         {"provider_id": "T4", "date": "2026-01-01", "amount": "x"}, "text", tx("T5", kind="weird", car_id=99),
         claims=[line(), "text", {"provider_id": "", "date": "2026-01-01", "amount": 1}, line("P2", day="nope"), line("P3", car_id=99, paid_date="junk")])
    rows = {r["provider_id"]: r for r in stored(admin)}
    assert set(rows) == {"T1", "T5"} and rows["T5"]["kind"] == "other" and rows["T5"]["car_id"] is None
    assert [c["provider_id"] for c in lines(admin)] == ["P0001", "P3"] and lines(admin)[1]["car_id"] is None


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


# ---- fetching only what is new, in the background -----------------------------------------------------------

def asked_since(fake):
    return last_call(fake, "/v1/transactions")[3].get("since")


def wait_idle(admin, seconds=20):
    import time
    for _ in range(int(seconds * 20)):
        row = admin.execute("SELECT state FROM garagebooks.provider_sync WHERE id = 1").fetchone()
        if row["state"] == "idle":
            return
        time.sleep(0.05)
    raise AssertionError("the sync never finished")


def test_the_first_sync_goes_back_to_the_first_expense_then_only_the_last_months(client, admin, fake):
    lease(admin)
    expense(admin, at="2026-03-10 04:00+00")
    sync(client, fake)
    assert asked_since(fake) == "2026-02-08"                       # a month before the earliest expense
    sync(client, fake, tx("T1", day="2026-11-07"), claims=[line(day="2026-11-01")])
    sync(client, fake, tx("T1", day="2026-11-07"))
    assert asked_since(fake) == "2026-08-09"                       # 90 days before the newest thing it holds
    client.post("/claims/sync", data={"full": "1"}, headers=ORIGIN)
    assert asked_since(fake) == "2026-02-08"                       # a full resync starts again


def test_a_catch_up_sync_leaves_the_older_history_alone(client, admin, fake):
    lease(admin)
    expense(admin, at="2026-01-10 04:00+00")
    sync(client, fake, tx("OLD", day="2026-01-15"), tx("NEW", day="2026-11-07"), claims=[line("P1", day="2026-01-12"), line("P2", line_id="L2", day="2026-11-01")])
    sync(client, fake, tx("NEW", day="2026-11-07"), claims=[line("P2", line_id="L2", day="2026-11-01")])   # asked from 90 days back
    assert {r["provider_id"] for r in stored(admin)} == {"OLD", "NEW"} and {c["provider_id"] for c in lines(admin)} == {"P1", "P2"}


def test_the_real_app_syncs_in_the_background_and_the_page_shows_it(client, admin, fake, tyres):
    client.application.testing = False
    fake.config["transactions"] = [tx("T1")]
    resp = client.post("/claims/sync", headers=ORIGIN)
    assert resp.status_code == 303 and "msg=claims_sync_started" in resp.headers["Location"]
    wait_idle(admin)
    assert [r["provider_id"] for r in stored(admin)] == ["T1"]
    state = client.get("/claims/sync/status").get_json()
    assert state == {"running": False, "outcome": "synced", "message": None}


def test_a_running_sync_shows_and_blocks_another(client, admin, fake):
    admin.execute("UPDATE garagebooks.provider_sync SET state = 'running', started_at = now(), since = '2026-01-01'")
    page = said(client.get("/claims"))
    assert 'id="sync-running"' in page and "Syncing the provider's history from 01 Jan 2026" in page and "disabled" in page
    assert client.get("/claims/sync/status").get_json()["running"] is True
    assert "msg=claims_sync_running" in client.post("/claims/sync", headers=ORIGIN).headers["Location"]
    assert not [c for c in fake.calls if c[1] == "/v1/transactions"]
    admin.execute("UPDATE garagebooks.provider_sync SET started_at = now() - interval '1 hour'")   # it died with its worker
    sync(client, fake, tx("T1"))
    assert len(stored(admin)) == 1


def test_a_background_sync_that_fails_says_so_and_a_signed_out_one_offers_the_sign_in(client, admin, fake):
    client.application.testing = False
    fake.config["override"] = {"transactions": (502, {"ok": False, "error": "The provider's site is down."})}
    client.post("/claims/sync", headers=ORIGIN)
    wait_idle(admin)
    assert "The last sync didn't finish: The provider's site is down." in said(client.get("/claims"))
    fake.config["override"] = {"transactions": (409, {"ok": False, "needs": "signin"})}
    client.post("/claims/sync", headers=ORIGIN)
    wait_idle(admin)
    assert 'id="claim-signin"' in said(client.get("/claims"))


# ---- claims to expenses, payments to claims -------------------------------------------------------------

def test_a_claim_made_from_the_app_is_the_expenses_claim_and_its_payments_follow_it(client, admin, fake, tyres):
    submit(client, tyres)                                         # the fake answers provider id P0001
    sync(client, fake, tx("T1", amount=600.0, day="2026-11-12", claim="P0001"), claims=[line(amount=1199.0, day="2026-11-08")])
    assert lines(admin)[0]["expense_id"] == tyres and stored(admin)[0]["expense_id"] == tyres
    assert f"/{tyres}/edit" in row_of(said(client.get("/claims")), "tx", "T1")


def test_a_payment_follows_its_claim_even_before_the_claim_history_is_synced(client, admin, fake, tyres):
    submit(client, tyres)
    sync(client, fake, tx("T1", amount=600.0, claim="P0001"))
    assert stored(admin)[0]["expense_id"] == tyres


def test_a_claim_made_at_the_provider_matches_one_expense_by_car_day_and_amount(client, admin, fake):
    lease(admin)
    a = expense(admin, amount="1200.00")
    b = expense(admin, amount="80.00", at="2026-11-08 04:05+00")
    sync(client, fake, tx("T1", claim="P1"), claims=[line("P1"), line("P2", amount=80.0, day="2026-11-08"),
                                                       line("P3", amount=77.0, day="2026-11-08")])
    got = {c["provider_id"]: c["expense_id"] for c in lines(admin)}
    assert got == {"P1": a, "P2": b, "P3": None} and stored(admin)[0]["expense_id"] == a


def test_a_claim_line_that_fits_two_expenses_is_left_unmatched(client, admin, fake):
    lease(admin)
    expense(admin, amount="80.00")
    expense(admin, amount="80.00")
    sync(client, fake, claims=[line(amount=80.0)])
    assert lines(admin)[0]["expense_id"] is None


def test_another_cars_expense_is_never_a_match(client, admin, fake):
    second_car(admin)
    expense(admin, car=2)
    sync(client, fake, claims=[line(car_id=1)])
    assert lines(admin)[0]["expense_id"] is None


def test_per_km_charging_needs_no_expense_and_is_not_flagged(client, admin, fake):
    lease(admin)
    sync(client, fake, tx("K1", kind="per_km_charging", amount=42.0), tx("T9", amount=5.0))
    page = said(client.get("/claims"))
    assert "no expense needed" in row_of(page, "match", "per-km").lower()
    assert "No matching expense" in row_of(page, "tx", "T9") and "No matching expense" not in row_of(page, "tx", "K1")
    only = said(client.get("/claims?tx=unmatched"))
    assert row_of(only, "tx", "T9") and not row_of(only, "tx", "K1")


def test_a_deleted_expense_frees_its_claim_and_payments(client, admin, fake):
    lease(admin)
    a = expense(admin)
    sync(client, fake, tx("T1", claim="P1"), claims=[line("P1")])
    assert stored(admin)[0]["expense_id"] == a
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (a,))
    assert stored(admin)[0]["expense_id"] is None and lines(admin)[0]["expense_id"] is None


def test_an_expense_claimed_at_the_provider_is_not_offered_for_claiming_again(client, admin, fake, tyres):
    sync(client, fake, claims=[line("P1")])
    page = said(client.get("/claims"))
    assert 'data-state="claimed"' in row_of(page, "expense", str(tyres)) and "claimed at the provider" in page
    assert f"/{tyres}/claim" not in page
    assert "already" in said(client.get(f"/{tyres}/claim")).lower() and not claims_made(fake)


def test_removing_a_claim_record_rematches(client, admin, fake, tyres):
    submit(client, tyres)
    sync(client, fake, tx("T1", amount=600.0, claim="P0001"), claims=[line(amount=1199.0, day="2026-11-08")])
    assert lines(admin)[0]["expense_id"] == tyres
    client.post(f"/{tyres}/claim/forget", headers=ORIGIN)
    assert lines(admin)[0]["expense_id"] is None and stored(admin)[0]["expense_id"] is None
    assert f"/{tyres}/claim" in said(client.get("/claims"))


# ---- the provider dates things its own way ------------------------------------------------------------------

def test_a_claim_dated_a_few_days_off_still_matches_when_amount_and_car_are_unambiguous(client, admin, fake):
    lease(admin)
    a = expense(admin, amount="1556.00", at="2026-10-24 04:00+00")
    sync(client, fake, claims=[line("P1", amount=1556.0, day="2026-10-22")])
    assert lines(admin)[0]["expense_id"] == a


def test_the_same_day_wins_and_look_alikes_are_left_unmatched(client, admin, fake):
    lease(admin)
    a = expense(admin, amount="80.00", at="2026-11-05 04:00+00")
    b = expense(admin, amount="80.00", at="2026-11-08 04:00+00")
    sync(client, fake, claims=[line("P1", amount=80.0, day="2026-11-05")])
    assert lines(admin)[0]["expense_id"] == a                                  # the exact day first
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (a,))
    expense(admin, amount="80.00", at="2026-11-12 04:00+00")
    sync(client, fake, claims=[line("P1", amount=80.0, day="2026-11-10")])
    assert lines(admin)[0]["expense_id"] is None                               # two fit: no guessing
    assert b                                                                   # (kept: the second expense exists)


def test_a_claim_more_than_a_month_off_is_not_matched(client, admin, fake):
    lease(admin)
    expense(admin, amount="1556.00", at="2026-10-24 04:00+00")
    sync(client, fake, claims=[line("P1", amount=1556.0, day="2026-11-25")])
    assert lines(admin)[0]["expense_id"] is None


def test_a_payment_the_provider_made_itself_matches_by_nearby_day_and_roughly_the_amount(client, admin, fake):
    lease(admin)
    rego = expense(admin, category="Registration", amount="716.00", at="2026-11-21 04:00+00")
    sync(client, fake, tx("R1", kind="direct_payment", amount=772.93, day="2026-11-29", description="Registration Renewal Program"))
    assert stored(admin)[0]["expense_id"] == rego
    row = re.sub(r"\s+", " ", row_of(said(client.get("/claims")), "expense", str(rego)))
    assert 'data-state="claimed"' in row and "Paid by the provider directly" in row and "Registration Renewal Program" in row
    assert f"/{rego}/claim" not in row


def test_a_direct_payment_too_far_off_in_time_or_amount_is_not_matched(client, admin, fake):
    lease(admin)
    expense(admin, category="Registration", amount="716.00", at="2026-11-21 04:00+00")
    sync(client, fake, tx("R1", kind="direct_payment", amount=772.93, day="2026-12-20"), tx("R2", kind="direct_payment", amount=1100.0, day="2026-11-22"))
    assert [r["expense_id"] for r in stored(admin)] == [None, None]


def test_deductions_and_other_rows_are_not_flagged_and_stay_out_of_the_default_list(client, admin, fake):
    sync(client, fake, tx("D1", kind="other", amount=253.3), tx("P9", amount=5.0))
    page = said(client.get("/claims"))
    assert row_of(page, "tx", "P9") and not row_of(page, "tx", "D1")
    assert not row_of(said(client.get("/claims?tx=unmatched")), "tx", "D1")
    other = said(client.get("/claims?tx=other"))
    assert "Not a claim" in row_of(other, "tx", "D1") and "No matching expense" not in row_of(other, "tx", "D1")


# ---- is the claim fully reimbursed? ---------------------------------------------------------------------

def pay_text(client, tyres):
    return re.sub(r"\s+", " ", row_of(said(client.get("/claims")), "expense", str(tyres)))


def test_part_payments_are_added_up_against_what_the_claim_should_pay(client, admin, fake, tyres):
    submit(client, tyres)
    claim = [line(amount=1200.0, approved=1100.0)]
    sync(client, fake, claims=claim)
    assert "Awaiting payment" in pay_text(client, tyres)
    sync(client, fake, tx("T1", amount=600.0, claim="P0001"), claims=claim)
    assert "Part paid: $500.00 to come" in pay_text(client, tyres)
    sync(client, fake, tx("T1", amount=600.0, claim="P0001"), tx("T2", amount=500.0, day="2026-11-20", claim="P0001"), claims=claim)
    assert "Reimbursed in full" in pay_text(client, tyres)
    sync(client, fake, tx("T1", amount=600.0, claim="P0001"), tx("T2", amount=600.0, day="2026-11-20", claim="P0001"), claims=claim)
    assert "Overpaid by $100.00" in pay_text(client, tyres)


def test_the_claims_filter_finds_what_isnt_reimbursed_in_full(client, admin, fake, tyres):
    submit(client, tyres)
    other = expense(admin, category="Insurance", amount="900.00", at="2026-10-01 04:00+00")
    submit(client, other)                                          # P0002
    sync(client, fake, tx("T1", amount=900.0, claim="P0002"),
         claims=[line(approved=1100.0), line("P0002", reference="REF0002", amount=900.0, day="2026-10-01")])
    page = said(client.get("/claims?state=unpaid"))
    assert row_of(page, "expense", str(tyres)) and not row_of(page, "expense", str(other))


# ---- every expense is on the page, listed as on the All page ----------------------------------------------

def test_before_the_history_is_synced_an_expense_is_not_called_ready(client, admin, fake, tyres):
    """It may well have been claimed at the provider already: the page says it hasn't checked, not that it is ready."""
    page = said(client.get("/claims"))
    assert "Not checked against the provider" in row_of(page, "expense", str(tyres)) and "Ready to claim" not in row_of(page, "expense", str(tyres))
    assert f"/{tyres}/claim" in page                              # the connector's preview still warns of a repeat
    sync(client, fake)                                            # an answer with nothing in it: nothing is claimed there
    sync(client, fake, tx("T9", amount=5.0))
    assert "Ready to claim" in row_of(said(client.get("/claims")), "expense", str(tyres))


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


def test_the_claims_page_uses_the_all_pages_list(client, admin, tyres):
    claims_page, all_page = said(client.get("/claims")), said(client.get("/"))
    for marker in ('class="card list"', 'class="filter"', 'name="sort"', 'a class="sort"', "data-view-choice", 'data-live'):
        assert marker in claims_page and marker in all_page, marker
    assert 'name="state"' in claims_page and 'name="state"' in all_page          # the Claims filter is on both
    assert "Where it stands" not in all_page and "Not checked against" not in all_page   # the long text is the Claims page's


# ---- per car and other filters, on both pages -------------------------------------------------------------

def test_the_provider_history_uses_the_same_rows_and_dates(client, admin, fake, tyres):
    sync(client, fake, tx("T1", day="2026-11-07"))
    page = said(client.get("/claims"))
    history = row_of(page, "tx", "T1")
    for cell in ('<td class="date"', '<td class="cat"', '<td class="vendor"', 'td class="num amount"', '<td class="claim-state"'):
        assert cell in history and cell in row_of(page, "expense", str(tyres)), cell
    assert "07 Nov 2026" in history and "07 Nov 2026" in row_of(page, "expense", str(tyres))     # one date format
    assert "strftime" not in page


def test_both_pages_filter_by_car(client, admin, fake, tyres):
    second_car(admin)
    other = expense(admin, car=2, amount="300.00")
    sync(client, fake, tx("T1"), tx("T2", amount=300.0, car_id=2))
    for path in ("/claims?car=2", "/?car=2"):
        page = said(client.get(path))
        assert row_of(page, "expense", str(other)) and not row_of(page, "expense", str(tyres)), path
    page = said(client.get("/claims?car=2"))
    assert row_of(page, "tx", "T2") and not row_of(page, "tx", "T1")
    both = said(client.get("/claims"))
    assert row_of(both, "expense", str(tyres)) and row_of(both, "expense", str(other))
    assert 'name="car"' in both and "Second car" in both
    assert client.get("/claims?car=abc").status_code == 200       # junk is ignored, not an error


def test_dates_filter_both_lists_and_junk_is_ignored(client, admin, fake, tyres):
    claimed = expense(admin, category="Insurance", amount="900.00", at="2026-10-01 04:00+00")
    submit(client, claimed)
    sync(client, fake, tx("T1", day="2026-09-01"), tx("T2", day="2026-11-09", amount=5.0))
    page = said(client.get("/claims?from=2026-10-15&to=2026-12-31"))
    assert row_of(page, "tx", "T2") and not row_of(page, "tx", "T1")
    assert row_of(page, "expense", str(tyres)) and not row_of(page, "expense", str(claimed))
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
        app.execute("INSERT INTO garagebooks.provider_claim (provider, provider_id, occurred_on, amount)"
                    " VALUES ('P', 'c', '2026-01-01', 1)")
        app.execute("UPDATE garagebooks.provider_claim SET status = 'Paid'")
        assert app.execute("DELETE FROM garagebooks.provider_claim").rowcount == 1


def test_a_cached_row_is_unique_per_provider_and_key(admin):
    for table in ("provider_transaction", "provider_claim"):
        q = f"INSERT INTO garagebooks.{table} (provider, provider_id, occurred_on, amount) VALUES ('P', 'x', '2026-01-01', 1)"
        admin.execute(q)
        with pytest.raises(psycopg.errors.UniqueViolation):
            admin.execute(q)
        admin.execute(f"DELETE FROM garagebooks.{table}")
