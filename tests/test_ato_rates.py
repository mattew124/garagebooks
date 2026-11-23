"""garagebooks/ato.py (and scripts/ato_rates.py): reads tax rates from the ATO's published pages, for the lease
what-if workbook and the app's tax brackets and home charging rate.

The pages are made-up snippets shaped like the ATO's, so nothing here touches the network. A change to
the ATO's layout must make the script fail loudly, never produce a quietly wrong table.
"""
import pytest

from garagebooks import ato


def table(caption, rows, head=("Taxable income", "Tax on this income")):
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f"<table><caption>{caption}</caption><thead><tr>" + "".join(f"<th>{h}</th>" for h in head)
            + f"</tr></thead><tbody>{body}</tbody></table>")


RESIDENT = (
    table("Resident tax rates 2026&ndash;27", [
        ["0 &ndash; $18,200", "Nil"],
        ["$18,201 &ndash; $45,000", "15c for each $1 over $18,200"],
        ["$45,001 &ndash; $135,000", "$4,020 plus 30c for each $1 over $45,000"],
        ["$135,001 &ndash; $190,000", "$31,020 plus 37c for each $1 over $135,000"],
        ["$190,001 and over", "$51,370 plus 45c for each $1 over $190,000"],
    ])
    + table("Resident tax rates 2022&ndash;23", [
        ["0 &ndash; $18,200", "Nil"],
        ["$18,201 &ndash; $45,000", "19c for each $1 over $18,200"],
        ["$45,001 &ndash; $120,000", "$5,092 plus 32.5c for each $1 over $45,000"],
        ["$120,001 &ndash; $180,000", "$29,467 plus 37c for each $1 over $120,000"],
        ["$180,001 and over", "$51,667 plus 45c for each $1 over $180,000"],
    ])
)

HELP = (
    table("Table&nbsp;1: 2026&ndash;27 repayment thresholds and rates", [
        ["$0 &ndash; $69,528", "Nil"],
        ["$69,529 &ndash; $129,717", "15c for each $1 over $69,528"],
        ["$129,718 &ndash; $186,050", "$9,028 plus 17c for each $1 over $129,717"],
        ["$186,051 and over", "10% of your total repayment income"],
    ], head=("Repayment income", "Repayment on this income"))
    + table("Table&nbsp;3: 2022&ndash;23 repayment income thresholds and rates", [
        ["Below $48,361", "Nil"],
        ["$48,361 &ndash; $55,836", "1.0%"],
        ["$55,837 &ndash; $59,186", "2.0%"],
        ["$141,848 and above", "10.0%"],
    ], head=("Repayment income", "Repayment rate"))
)

MEDICARE = (
    "<p>In 2025&ndash;26, you don't have to pay the Medicare levy if your taxable income is at or below the lower threshold.</p>"
    + table("Medicare levy thresholds for a single individual", [
        ["If you're entitled to the Seniors and pensioners tax offset", "$44,268", "$55,335 (see note 1)"],
        ["All other taxpayers", "$28,011", "$35,013"],
    ], head=("Category", "Lower threshold", "Upper threshold"))
)

FAMILY = (
    "<p>You may qualify if you meet both of the following in 2025-26: your individual taxable income was more than $35,013.</p>"
    "<p>The lower family income threshold is either: $47,238 $61,623 if you\u2019re entitled to the SAPTO. "
    "The lower family income threshold increases by $4,338 for each dependent child. "
    "The upper family income threshold is either: $59,047 $77,028 if you're entitled to SAPTO (if your entitlement "
    "to the SAPTO reduces to zero before you reach the upper limit you must use the non SAPTO thresholds). "
    "The upper income threshold increases by $5,423 for each dependent child.</p>"
)

LITO = (
    "<p>If you earned: $37,500 or less, you will get the maximum offset of $700 "
    "between $37,501 and $45,000, you will get $700 minus 5 cents for every $1 above $37,500 "
    "between $45,001 and $66,667, you will get $325 minus 1.5 cents for every $1 above $45,000.</p>"
)


# ---- income tax -----------------------------------------------------------------

def test_resident_rates_are_read_per_year():
    rates = ato.parse_resident(RESIDENT)
    assert list(rates) == ["2026-27", "2022-23"]
    assert [r["rate"] for r in rates["2026-27"]] == [0, 0.15, 0.30, 0.37, 0.45]
    assert [r["base"] for r in rates["2026-27"]] == [0, 0, 4020, 31020, 51370]
    assert [r["over"] for r in rates["2026-27"]] == [0, 18200, 45000, 135000, 190000]


def test_a_fractional_rate_is_kept():
    assert ato.parse_resident(RESIDENT)["2022-23"][2]["rate"] == 0.325


def test_a_bracket_starts_half_a_dollar_below_its_first_whole_dollar():
    # so a lookup of 18,201 lands in the 16c bracket and 18,200 doesn't
    starts = [r["start"] for r in ato.parse_resident(RESIDENT)["2026-27"]]
    assert starts == [0, 18200.5, 45000.5, 135000.5, 190000.5]


def test_tax_follows_the_ato_worked_figures():
    rows = ato.parse_resident(RESIDENT)["2026-27"]
    assert ato.tax_on(100000, rows) == pytest.approx(4020 + 0.30 * 55000)
    assert ato.tax_on(18200, rows) == 0
    assert ato.tax_on(200000, rows) == pytest.approx(51370 + 0.45 * 10000)


# ---- HELP -----------------------------------------------------------------------

def test_marginal_help_rows_have_a_base_a_rate_and_a_threshold():
    rows = ato.parse_help(HELP)["2026-27"]
    assert [(r["base"], r["rate"], r["over"], r["total"]) for r in rows] == [
        (0, 0, 0, False), (0, 0.15, 69528, False), (9028, 0.17, 129717, False), (0, 0.10, 0, True)]


def test_older_help_years_are_a_percentage_of_all_income():
    rows = ato.parse_help(HELP)["2022-23"]
    assert [(r["rate"], r["total"]) for r in rows] == [(0, False), (0.01, True), (0.02, True), (0.10, True)]
    assert rows[1]["start"] == 48360.5


def test_help_repayment_either_way():
    marginal = ato.parse_help(HELP)["2026-27"]
    assert ato.help_on(60000, marginal) == 0
    assert ato.help_on(100000, marginal) == pytest.approx(0.15 * (100000 - 69528))
    assert ato.help_on(200000, marginal) == pytest.approx(20000)
    percent = ato.parse_help(HELP)["2022-23"]
    assert ato.help_on(50000, percent) == pytest.approx(500)


# ---- Medicare and LITO ----------------------------------------------------------

def test_medicare_thresholds_for_a_single_person():
    m = ato.parse_medicare(MEDICARE)
    assert (m["year"], m["lower"], m["upper"]) == ("2025-26", 28011, 35013)


def test_lito_steps():
    steps = ato.parse_lito(LITO)
    assert [(s["start"], s["base"], s["rate"], s["over"]) for s in steps] == [
        (0, 700, 0, 0), (37500.5, 700, 0.05, 37500), (45000.5, 325, 0.015, 45000), (66667.5, 0, 0, 0)]


def test_medicare_family_thresholds():
    f = ato.parse_medicare_family(FAMILY)
    assert f == {"year": "2025-26", "lower": 47238, "upper": 59047, "lower_child": 4338, "upper_child": 5423}


# ---- a changed page fails loudly -------------------------------------------------

@pytest.mark.parametrize("parse, page", [
    (ato.parse_resident, "<p>We have moved this page.</p>"),
    (ato.parse_help, "<p>We have moved this page.</p>"),
    (ato.parse_medicare, "<p>We have moved this page.</p>"),
    (ato.parse_medicare_family, "<p>We have moved this page.</p>"),
    (ato.parse_lito, "<p>We have moved this page.</p>"),
])
def test_a_page_without_the_tables_is_an_error(parse, page):
    with pytest.raises(ato.RatesError):
        parse(page)


def test_a_bracket_out_of_order_is_an_error():
    bad = table("Resident tax rates 2026&ndash;27", [
        ["0 &ndash; $18,200", "Nil"],
        ["$45,001 &ndash; $135,000", "$4,020 plus 30c for each $1 over $45,000"],
        ["$18,201 &ndash; $45,000", "15c for each $1 over $18,200"],
        ["$135,001 &ndash; $190,000", "$31,020 plus 37c for each $1 over $135,000"],
        ["$190,001 and over", "$51,370 plus 45c for each $1 over $190,000"],
    ])
    with pytest.raises(ato.RatesError):
        ato.parse_resident(bad)


def test_a_tax_figure_that_does_not_add_up_is_an_error():
    # $4,020 is what the 15c bracket accumulates by $45,000; any other figure means a misread page
    bad = RESIDENT.replace("$4,020 plus 30c", "$9,999 plus 30c")
    with pytest.raises(ato.RatesError):
        ato.parse_resident(bad)


def test_a_rate_that_is_not_a_percentage_is_an_error():
    with pytest.raises(ato.RatesError):
        ato.parse_resident(RESIDENT.replace("15c for each", "150c for each"))


# ---- putting it together ---------------------------------------------------------

def test_fetch_all_reads_five_pages_and_says_where_from():
    pages = {ato.URLS["resident"]: RESIDENT, ato.URLS["help"]: HELP,
             ato.URLS["medicare"]: MEDICARE, ato.URLS["lito"]: LITO, ato.URLS["medicare_family"]: FAMILY}
    got = ato.fetch_all(lambda url: pages[url], now="2026-11-15")
    assert got["fetched"] == "2026-11-15"
    assert got["sources"] == {key: ato.URLS[key] for key in ato.WORKBOOK_PAGES}
    assert set(got["tax"]) == {"2026-27", "2022-23"}
    assert set(got["help"]) == {"2026-27", "2022-23"}
    assert got["medicare"]["lower"] == 28011 and got["lito"][1]["over"] == 37500
    assert got["medicare_family"]["upper_child"] == 5423


def test_only_ato_addresses_over_https_are_fetched():
    assert all(u.startswith("https://www.ato.gov.au/") for u in ato.URLS.values())
    with pytest.raises(ato.RatesError):
        ato.get("http://www.ato.gov.au/anything")
    with pytest.raises(ato.RatesError):
        ato.get("https://example.com/anything")
