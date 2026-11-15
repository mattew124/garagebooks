"""scripts/build_lease_workbook.py: the lease what-if workbook, built from the ATO's published rates.

The figures in it are invented and the rates are made-up pages' worth, so nothing here touches the
network. Numbers are not recalculated here (that needs a spreadsheet engine); what is checked is
that the workbook is wired to the rates and to the choices a person makes.
"""
import importlib.util
from pathlib import Path

import pytest
from openpyxl import load_workbook

from tests import test_ato_rates as pages

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_lease_workbook", ROOT / "scripts" / "build_lease_workbook.py")
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

PAGES = {pages.ato.URLS["resident"]: pages.RESIDENT, pages.ato.URLS["help"]: pages.HELP,
         pages.ato.URLS["medicare"]: pages.MEDICARE, pages.ato.URLS["lito"]: pages.LITO,
         pages.ato.URLS["medicare_family"]: pages.FAMILY}


@pytest.fixture(scope="module")
def rates():
    return pages.ato.fetch_all(lambda url: PAGES[url], now="2026-11-15")


@pytest.fixture(scope="module")
def wb(rates, tmp_path_factory):
    out = tmp_path_factory.mktemp("wb") / "lease.xlsx"
    bw.build(rates, out)
    return load_workbook(out)


def validations(ws):
    return {str(rng): dv for dv in ws.data_validations.dataValidation for rng in dv.sqref.ranges}


def test_the_sheets(wb):
    assert wb.sheetnames == ["Scenario", "Inputs", "Rates", "RateData"]


def test_rates_say_where_they_came_from_and_when(wb, rates):
    text = " ".join(str(c.value) for row in wb["Rates"].iter_rows() for c in row if c.value)
    assert "2026-11-15" in text
    assert all(url in text for url in rates["sources"].values())


def test_the_years_with_rates_are_the_ones_every_table_has_newest_first(wb):
    years = [c.value for c in wb["RateData"]["N"][1:] if c.value]
    assert years == ["2026-27", "2022-23"]


def test_the_tax_year_comes_from_the_lease_start_date_not_a_dropdown(wb):
    ws = wb["Inputs"]
    assert "B3" not in validations(ws)
    assert ws["B3"].value.date() == bw.DEFAULT_START
    assert "start date" in ws["A3"].value.lower()
    rates = wb["Rates"]
    assert "Inputs!$B$3" in rates["B9"].value and "Inputs!$B$3" in rates["B11"].value
    assert rates["B11"].value.endswith("+2")     # the third year of the lease


def test_a_year_the_ato_has_not_published_falls_back_and_says_so(wb):
    rates = wb["Rates"]
    assert "has no" in rates["F10"].value and "yet" in rates["F10"].value
    assert "COUNTA" in rates["D10"].value        # before the oldest year: the oldest; after the newest: the newest


def test_each_lease_year_looks_up_its_own_years_rates(wb):
    sc = wb["Scenario"]
    for col, tax_rows, help_rows in (("B", "$A$17:$D$21", "$A$43:$E$67"), ("E", "$A$25:$D$29", "$A$71:$E$95"),
                                      ("H", "$A$33:$D$37", "$A$99:$E$123")):
        assert f"Rates!{tax_rows}" in sc[f"{col}15"].value and f"Rates!{tax_rows}" in sc[f"{col}33"].value
        assert f"Rates!{help_rows}" in sc[f"{col}18"].value and f"Rates!{help_rows}" in sc[f"{col}36"].value


def test_the_ato_tax_figures_are_in_the_data(wb, rates):
    ws = wb["RateData"]
    first = [[c.value for c in row[:5]] for row in ws.iter_rows(min_row=2, max_row=6, max_col=5)]
    assert [r[0] for r in first] == ["2026-27"] * 5
    assert [r[1:] for r in first][2] == [45000.5, 4020.0, 0.30, 45000.0]
    second = [ws.cell(r, 1).value for r in range(7, 12)]
    assert second == ["2022-23"] * 5


def test_help_blocks_are_the_same_size_for_every_year(wb):
    ws = wb["RateData"]
    block = bw.HELP_ROWS
    assert ws.cell(2, 7).value == "2026-27" and ws.cell(2 + block, 7).value == "2022-23"
    first = [ws.cell(2 + i, 8).value for i in range(block)]
    assert first[:4] == [0.0, 69528.5, 129717.5, 186050.5]
    assert all(v == 1e99 for v in first[4:])     # padding never matches an income


def test_each_person_picks_a_tax_bracket_not_a_salary(wb):
    ws = wb["Inputs"]
    v = validations(ws)
    for cell in ("B28", "C28"):
        assert v[cell].type == "list" and v[cell].formula1 == "=Rates!$E$17:$E$21"
    assert ws["B28"].value == "$45,001 - $135,000 (30%)"
    assert ws["C28"].value == "$18,201 - $45,000 (15%)"
    assert ws["B29"].value is None and ws["C29"].value is None   # no salaries
    assert "Gross salary" in ws["A29"].value


def test_the_default_brackets_are_in_the_first_lease_years_list(wb):
    labels = bw.bracket_labels(PAGES_RATES_2026)
    assert wb["Inputs"]["B28"].value in labels and wb["Inputs"]["C28"].value in labels


def test_the_year_for_a_start_year():
    years = ["2026-27", "2024-25", "2022-23"]
    assert bw.rates_year(2024, years) == "2024-25"
    assert bw.rates_year(2030, years) == "2026-27"      # not published yet: the newest
    assert bw.rates_year(2015, years) == "2022-23"      # before the first: the oldest


RULE = "Exempt (full or half by ATO rule)"


def test_medicare_is_an_exempt_or_not_choice_for_each_person(wb):
    ws = wb["Inputs"]
    v = validations(ws)
    for cell in ("B33", "C33"):
        assert v[cell].type == "list" and v[cell].formula1 == f'"Not exempt,Half exempt,Exempt,{RULE}"'
        assert ws[cell].value == "Not exempt"
    assert "Exempt" in ws["B34"].value and ws["C34"].value.startswith("=IF(C33")


def test_the_dependants_that_decide_the_exemption_are_choices(wb):
    ws = wb["Inputs"]
    v = validations(ws)
    for col in "BC":
        assert v[f"{col}37"].formula1 == '"No dependants,Partner only,Partner and shared children,Partner and other dependants"'
        assert ws[f"{col}37"].value == "Partner only"
        assert v[f"{col}38"].formula1 == '"None,Claim full,Claim half"' and ws[f"{col}38"].value == "None"
    assert ws["B8"].value == 0 and "children" in ws["A8"].value.lower()


def test_the_exemption_rule_follows_the_atos_table(wb):
    """Partner in an exemption category or paying the levy: full. Shared children: full only if the partner pays
    (or by a family agreement). Other dependants who pay no levy: half. No dependants: full."""
    f = wb["Scenario"]["B16"].value
    assert f'Inputs!B33="{RULE}"' in f
    assert 'Inputs!B37="No dependants"' in f and 'Inputs!B37="Partner and other dependants"' in f
    assert 'Inputs!B37="Partner and shared children"' in f and 'Inputs!B38="Claim full"' in f
    assert 'Inputs!C33<>"Not exempt"' in f                  # a partner in an exemption category counts
    assert "C14>Rates!$B$127" in f and "Inputs!C34>0" in f   # a partner who has to pay the levy counts
    with_lease = wb["Scenario"]["B34"].value
    assert "C32>Rates!$B$127" in with_lease and 'Inputs!C33<>"Not exempt"' in with_lease
    assert "B14>Rates!$B$127" in wb["Scenario"]["C16"].value


def test_both_people_claiming_the_full_exemption_by_agreement_is_flagged(wb):
    ws = wb["Inputs"]
    assert "agreement" in ws["B39"].value.lower() and 'B38="Claim full"' in ws["B39"].value and "C38" in ws["B39"].value


def test_no_levy_at_or_under_the_family_threshold(wb):
    """The ATO: no levy if family income is at or under the family threshold (more with dependent children)."""
    f = wb["Scenario"]["B16"].value
    assert "Rates!$B$132+Inputs!$B$8*Rates!$B$134" in f and "B14+C14" in f
    assert "B32+C32" in wb["Scenario"]["B34"].value
    rates = wb["Rates"]
    assert "family" in rates["A132"].value.lower() and rates["B132"].value == "=RateData!Y2"
    assert rates["B133"].value == "=RateData!Z2" and rates["B134"].value == "=RateData!AA2"


def test_between_the_family_thresholds_the_workbook_says_it_has_not_reduced_the_levy(wb):
    sc = wb["Scenario"]
    for cell in ("B45", "B46"):
        assert "reduction may apply" in sc[cell].value and "not reduced" in sc[cell].value
        assert "Rates!$B$133+Inputs!$B$8*Rates!$B$135" in sc[cell].value


def test_the_partner_rule_looks_at_the_partners_income_with_and_without_the_lease(wb):
    sc = wb["Scenario"]
    assert "C14>Rates!$B$127" in sc["B16"].value and "C32>Rates!$B$127" in sc["B34"].value
    assert "B14>Rates!$B$127" in sc["C16"].value and "B32>Rates!$B$127" in sc["C34"].value


def test_the_partner_rule_cannot_loop(wb):
    # a person's levy depends on the partner's taxable income, never on the partner's levy
    import re
    sc = wb["Scenario"]
    own_sheet = lambda f: set(re.findall(r"(?<![!$A-Z])([A-Z]{1,2}\d+)\b", f))      # cells on this sheet, not Inputs!/Rates!
    assert not own_sheet(sc["B16"].value) & {"C16", "C34"} and not own_sheet(sc["B34"].value) & {"C16", "C34"}
    assert not own_sheet(sc["C16"].value) & {"B16", "B34"} and not own_sheet(sc["C34"].value) & {"B16", "B34"}
    assert own_sheet(sc["B16"].value) >= {"B14", "C14"}          # it does read the partner's taxable income


def test_lease_terms_sit_below_the_people(wb):
    assert wb["Inputs"]["A42"].value.startswith("Lease terms")
    assert "Inputs!$A$44:$B$48" in wb["Scenario"]["B9"].value


def test_depreciation_is_capped_at_the_car_limit(wb):
    sc, inp = wb["Scenario"], wb["Inputs"]
    assert "limit" in inp["A24"].value.lower() and inp["B24"].value == 69674
    assert sc["B11"].value == "=MIN(B3,Inputs!B24)*Inputs!B23"
    assert sc["C11"].value == "=MIN(C3,Inputs!C24)*Inputs!C23"


def test_tax_is_looked_up_in_the_active_year_not_typed_in(wb):
    f = wb["Scenario"]["B15"].value
    assert "Rates!$A$17:$D$21" in f


def test_nothing_personal_in_the_workbook(wb, rates, tmp_path):
    """Only the example's own words and the ATO's rates: no comments, no author, no address, no salaries."""
    out = tmp_path / "lease.xlsx"
    bw.build(rates, out)
    import zipfile
    parts = zipfile.ZipFile(out).namelist()
    assert [p for p in parts if "comment" in p.lower() or "person" in p.lower() or "external" in p.lower()] == []
    strings = [str(c.value) for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    assert [s for s in strings if "@" in s] == []
    assert wb.properties.creator == "GarageBooks" and wb.properties.lastModifiedBy == "GarageBooks"
    for ws in wb:      # nobody's salary, debt or car value is a typed-in figure outside the invented inputs
        assert all(c.comment is None for row in ws.iter_rows() for c in row)
    inputs = wb["Inputs"]
    assert inputs["A1"].value == "Invented example figures. Blue cells are inputs."
    assert inputs["B9"].value == "Car A" and inputs["C9"].value == "Car B"
    assert inputs["B27"].value.startswith("Person A") and inputs["C27"].value.startswith("Person B")


PAGES_RATES_2026 = pages.ato.parse_resident(pages.RESIDENT)["2026-27"]
