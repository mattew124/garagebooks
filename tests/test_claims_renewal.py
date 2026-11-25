"""Asking the provider to pay a registration (its renewal program), as against claiming back one already paid.
The connector is tests/fake_connector.py; all values are invented."""

import json
import re

import pytest

from tests.test_claims import (  # noqa: F401 - fixtures used by name
    ORIGIN, client, expense, fake, last, lease, said)
from tests.test_claims_history import sync, tx

REFS = {"card_payment_ref": "1111 2222 33", "bpay_biller_code": "12345", "bpay_ref": "9876 5432"}


def registration(admin, renewed_by_provider=True, **over):
    lease(admin)
    admin.execute("UPDATE garagebooks.lease SET rego_renewed_by_provider=%s", (renewed_by_provider,))
    eid = expense(admin, category="Registration", amount="500.00", **over)
    admin.execute("UPDATE garagebooks.expense SET gst_amount=18.18, period_start='2026-12-01', period_end='2027-11-30',"
                  " card_payment_ref=%(card_payment_ref)s, bpay_biller_code=%(bpay_biller_code)s, bpay_ref=%(bpay_ref)s"
                  " WHERE id=%(id)s", {**REFS, "id": eid})
    return eid


def calls(fake, *paths):
    return [c[1] for c in fake.calls if c[1] in paths]


CLAIM_PATHS = ("/v1/preview", "/v1/submit", "/v1/renewal/preview", "/v1/renewal/submit")


def send(client, eid, **data):
    return client.post(f"/{eid}/claim/submit", data={"method": "renewal", "accept_declaration": "1", **data},
                       headers=ORIGIN)


def test_the_leases_setting_picks_the_renewal_request(client, admin, fake):
    eid = registration(admin)
    page = said(client.get(f"/{eid}/claim"))
    assert calls(fake, *CLAIM_PATHS) == ["/v1/renewal/preview"]
    assert last(fake, "/v1/renewal/preview")[3]["expense"]["fields"] == {**REFS, "ctp_gst": "18.18"}
    assert 'name="method" value="renewal"' in page and "I accept the declaration" in page
    assert "Example declaration" in page and "Send renewal request" in page
    for key, value in REFS.items():
        assert f'name="field_{key}" value="{value}"' in page
    assert "field_gst" not in page                                      # the claim's GST box isn't part of it


def test_without_the_setting_a_registration_is_claimed_back_and_either_can_be_chosen(client, admin, fake):
    eid = registration(admin, renewed_by_provider=False)
    page = said(client.get(f"/{eid}/claim"))
    assert calls(fake, *CLAIM_PATHS) == ["/v1/preview"]
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"gst": "18.18", "start": "2026-12-01", "end": "2027-11-30"}
    assert f"/{eid}/claim?method=renewal" in page and f"/{eid}/claim?method=reimburse" in page
    fake.calls.clear()
    client.get(f"/{eid}/claim?method=renewal")
    assert calls(fake, *CLAIM_PATHS) == ["/v1/renewal/preview"]


def test_only_a_registration_offers_the_renewal(client, admin, fake):
    lease(admin)
    admin.execute("UPDATE garagebooks.lease SET rego_renewed_by_provider=true")
    eid = expense(admin)
    page = said(client.get(f"/{eid}/claim?method=renewal"))
    assert "method=renewal" not in page
    send(client, eid)
    assert not calls(fake, "/v1/renewal/preview", "/v1/renewal/submit")


def test_what_the_provider_asks_for_is_typed_on_the_page(client, admin, fake):
    eid = registration(admin)
    fake.config["needs_fields"] = [{"key": "licence_number", "label": "Driver licence number", "type": "text"}]
    page = said(client.get(f"/{eid}/claim"))
    assert "Driver licence number" in page and 'name="field_licence_number"' in page
    page = said(client.post(f"/{eid}/claim", data={"method": "renewal", "field_licence_number": "000111222",
                                                     "field_bpay_ref": "5555"}, headers=ORIGIN))
    sent = last(fake, "/v1/renewal/preview")[3]["expense"]["fields"]
    assert sent["licence_number"] == "000111222" and sent["bpay_ref"] == "5555"
    assert 'name="field_licence_number" value="000111222"' in page      # carried on to the submit


def test_it_is_not_sent_without_accepting_the_declaration(client, admin, fake):
    eid = registration(admin)
    resp = client.post(f"/{eid}/claim/submit", data={"method": "renewal"}, headers=ORIGIN)
    assert resp.status_code == 400 and "accept the declaration" in said(resp)
    assert not calls(fake, "/v1/renewal/submit", "/v1/submit")


def test_no_declaration_from_the_connector_means_no_send_button(client, admin, fake):
    eid = registration(admin)
    fake.config["override"] = {"renewal_preview": (200, {"ok": True, "provider": "Example", "summary": ["x"]})}
    assert "Send renewal request" not in said(client.get(f"/{eid}/claim"))


def test_a_connector_without_the_renewal_never_gets_a_reimbursement_instead(client, admin, fake):
    eid = registration(admin)
    missing = (404, {"ok": False, "error": "no such endpoint"})
    fake.config["override"] = {"renewal_preview": missing, "renewal_submit": missing}
    assert "can't send a registration renewal" in said(client.get(f"/{eid}/claim"))
    assert "can't send a registration renewal" in said(send(client, eid))
    assert not calls(fake, "/v1/preview", "/v1/submit")


def test_a_sent_request_is_recorded_without_the_licence_or_references(client, admin, fake):
    eid = registration(admin)
    fields = {f"field_{k}": v for k, v in {**REFS, "ctp_gst": "18.18", "licence_number": "000111222"}.items()}
    resp = send(client, eid, **fields)
    assert resp.status_code == 303
    body = last(fake, "/v1/renewal/submit")[3]
    assert body["confirm"] is True and body["accept_declaration"] is True
    assert body["expense"]["fields"]["licence_number"] == "000111222" and body["invoice"]
    row = admin.execute("SELECT * FROM garagebooks.expense_claim").fetchone()
    assert (row["status"], row["fields"]) == ("Renewal requested", {"method": "renewal"})
    everything = json.dumps(admin.execute("SELECT before_row, after_row FROM garagebooks.change_log"
                                          " WHERE table_name='expense_claim'").fetchall(), default=str)
    assert "000111222" not in everything and "9876 5432" not in everything
    assert "Renewal request sent" in said(client.get(resp.headers["Location"]))
    assert "Renewal requested from" in said(client.get(f"/{eid}/edit"))
    assert send(client, eid).status_code == 400                         # never twice


@pytest.mark.parametrize("days_later", [3, 45])
def test_the_providers_own_payment_matches_the_renewal(client, admin, fake, days_later):
    eid = registration(admin, at="2026-11-07 04:05+00")
    send(client, eid)
    day = {3: "2026-11-10", 45: "2026-12-22"}[days_later]              # paid when due, weeks after the notice
    sync(client, fake, tx("D1", day=day, amount=500.0, kind="direct_payment"))
    assert admin.execute("SELECT expense_id FROM garagebooks.provider_transaction").fetchone()["expense_id"] == eid
    page = said(client.get("/claims"))
    assert "paid by the provider" in page.lower()
    unpaid = said(client.get("/?state=unpaid"))           # the same list as the Claims page, without its history
    assert not re.search(rf'href="[^"]*/{eid}/edit', unpaid)
