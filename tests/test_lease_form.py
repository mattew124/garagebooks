"""Reading a provider's lease form (an associate lease form, as a salary packaging provider issues it) to
fill in the Add a lease page. The fixture is the layout of a real form with every value invented."""

from datetime import date
from pathlib import Path

from garagebooks import leaseform

TEXT = (Path(__file__).parent / "fixtures" / "associate-lease-form.txt").read_text()


def test_reads_the_lease_details_from_an_associate_lease_form():
    found = leaseform.read(TEXT)
    assert found == {
        "associate_lease": True,
        "package_name": "Example Packaging",
        "start_date": "2026-07-01", "end_date": "2028-06-30", "term_months": "24",
        "pay_frequency": "fortnightly", "pretax_payment": "500.00",
        "fbt_base_value": "61000.00", "ev_exempt": True,
        "estimated_km_per_year": "15000",
        "budget_lease": "13000.00", "budget_maintenance": "250.00", "budget_registration": "1850.00",
        "budget_insurance": "1150.00", "budget_fuel_charging": "600.00",
    }


def test_nothing_personal_is_read():
    found = " ".join(str(v) for v in leaseform.read(TEXT).values())
    for private in ("Alex", "Sample", "0400", "000-000", "000AAA", "Example Employer"):
        assert private not in found


def test_text_that_is_not_a_lease_form_gives_nothing():
    assert leaseform.read("") == {}
    assert leaseform.read("Tax invoice\nTotal  $120.00\nDate 01/02/2026\n") == {}


def test_the_pay_frequency_comes_from_the_yearly_payment_and_the_instalment():
    for instalment, frequency in (("250.00", "weekly"), ("500.00", "fortnightly"), ("1,083.33", "monthly")):
        text = f"Lease Start Date  01/07/2026  Lease End Date  30/06/2029\nAnnual Lease Payment  $13,000.00\nInstalment Amount  $ {instalment}\n"
        assert leaseform.read(text)["pay_frequency"] == frequency
    odd = "Lease Start Date  01/07/2026\nAnnual Lease Payment  $13,000.00\nInstalment Amount  $ 700.00\n"
    assert "pay_frequency" not in leaseform.read(odd)


def test_dates_in_the_forms_a_provider_writes_them():
    for written in ("01-Jul-26", "01-Jul-2026", "01/07/2026", "1 July 2026", "2026-07-01"):
        assert leaseform.parse_date(written) == date(2026, 7, 1), written
    assert leaseform.parse_date("31-Feb-26") is None


def test_the_term_is_whole_months_between_the_dates():
    assert leaseform.term_months(date(2026, 7, 1), date(2028, 6, 30)) == 24
    assert leaseform.term_months(date(2026, 7, 1), date(2029, 6, 30)) == 36
    assert leaseform.term_months(date(2026, 7, 1), date(2026, 7, 20)) == 1
