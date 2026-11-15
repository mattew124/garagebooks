#!/usr/bin/env python3
"""Build the novated lease what-if workbook, with the tax rates read live from the ATO.

    scripts/build_lease_workbook.py [--rates rates.json] [--out docs/assets/lease-calculator-example.xlsx]

Without --rates it reads the ATO's pages now (scripts/ato_rates.py). Run it again whenever the ATO
publishes new rates and the workbook is current again. Needs openpyxl (requirements-dev.txt).

Everything in the workbook is invented: two example cars and two people, each person described by
the tax bracket they pick, not by a salary. Nothing of anyone's is read from anywhere.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

HELP_ROWS = 25        # HELP rows per year: the most any year has is 19, the rest is padding
TAX_ROWS = 5
NEVER = 1e99          # a padding row's start: no income reaches it
PARTNER_RULE = "Exempt (full or half by ATO rule)"
# The ATO's category 1 exemption (medical-exemption-from-medicare-levy, table 1). Full if there are no dependants,
# or each dependant including the partner is in an exemption category or has to pay the levy; for children shared
# with the partner, full only if the partner pays the levy (or by a family agreement); half if any other dependant
# is neither exempt nor paying.
DEPENDANTS = "No dependants,Partner only,Partner and shared children,Partner and other dependants"
AGREEMENT = "None,Claim full,Claim half"
MEDICARE_CHOICES = f"Not exempt,Half exempt,Exempt,{PARTNER_RULE}"
DEFAULT_START = date(2026, 10, 1)    # an invented lease start date
# Where each lease year's tables sit on the Rates sheet (first data row). Each of the three years looks up
# the rates for the income tax year its start date falls in.
TAX_FIRST = {1: 17, 2: 25, 3: 33}
HELP_FIRST = {1: 43, 2: 71, 3: 99}
MEDICARE_AT, LITO_FIRST, SBITO_AT = 126, 141, 146

BOLD = Font(bold=True)
INPUT = PatternFill("solid", fgColor="DDEBF7")
HEAD = PatternFill("solid", fgColor="E7E6E6")
MONEY = "$#,##0;-$#,##0"


def pct(rate):
    return f"{rate:.1%}".replace(".0%", "%")


def tax_year_label(start_year):
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def rates_year(start_year, years):
    """The ATO year whose rates apply to an income tax year starting in start_year (years: newest first).
    A year the ATO hasn't published yet uses the newest; one before the first uses the oldest."""
    label = tax_year_label(start_year)
    if label in years:
        return label
    return years[0] if start_year > int(years[0][:4]) else years[-1]


def start_year_of(start):
    return start.year if start.month > 6 else start.year - 1


def bracket_labels(rows):
    """'$45,001 - $135,000 (30%)' for each income tax bracket: what a person picks from."""
    out = []
    for i, row in enumerate(rows):
        low = 0 if row["start"] == 0 else round(row["start"] + 0.5)
        if i + 1 < len(rows):
            out.append(f"${low:,} - ${round(rows[i + 1]['start'] - 0.5):,} ({pct(row['rate'])})")
        else:
            out.append(f"${low:,} and over ({pct(row['rate'])})")
    return out


def put(ws, cell, value, fill=None, bold=False, fmt=None):
    c = ws[cell]
    c.value = value
    if fill:
        c.fill = fill
    if bold:
        c.font = BOLD
    if fmt:
        c.number_format = fmt
    return c


# ---- RateData: every year the ATO publishes, one block per year ----------------------

def rate_data(wb, rates):
    ws = wb.create_sheet("RateData")
    years = sorted(set(rates["tax"]) & set(rates["help"]), reverse=True)
    if not years:
        raise ValueError("no tax year has both income tax and HELP rates")
    for year in years:
        if len(rates["tax"][year]) != TAX_ROWS:
            raise ValueError(f"{year}: expected {TAX_ROWS} income tax brackets, found {len(rates['tax'][year])}")
        if len(rates["help"][year]) > HELP_ROWS:
            raise ValueError(f"{year}: more than {HELP_ROWS} HELP rows")
    for col, name in zip("ABCDE", ("Year", "Start", "Base", "Rate", "Over"), strict=True):
        put(ws, f"{col}1", name, bold=True)
    for col, name in zip("GHIJKL", ("Year", "Start", "Base", "Rate", "Over", "Total"), strict=True):
        put(ws, f"{col}1", name, bold=True)
    put(ws, "N1", "Tax years", bold=True)
    for i, year in enumerate(years):
        for k, row in enumerate(rates["tax"][year]):
            r = 2 + i * TAX_ROWS + k
            for col, v in zip("ABCDE", (year, row["start"], row["base"], row["rate"], row["over"]), strict=True):
                ws[f"{col}{r}"] = v
        for k in range(HELP_ROWS):
            r = 2 + i * HELP_ROWS + k
            row = rates["help"][year][k] if k < len(rates["help"][year]) else \
                {"start": NEVER, "base": 0.0, "rate": 0.0, "over": 0.0, "total": False}
            for col, v in zip("GHIJKL", (year, row["start"], row["base"], row["rate"], row["over"],
                                         1 if row["total"] else 0), strict=True):
                ws[f"{col}{r}"] = v
        ws[f"N{2 + i}"] = year
    put(ws, "P1", "Medicare year", bold=True)
    put(ws, "Q1", "Lower", bold=True)
    put(ws, "R1", "Upper", bold=True)
    m = rates["medicare"]
    ws["P2"], ws["Q2"], ws["R2"] = m["year"], m["lower"], m["upper"]
    for col, name in zip("TUVW", ("LITO start", "Base", "Rate", "Over"), strict=True):
        put(ws, f"{col}1", name, bold=True)
    for k, row in enumerate(rates["lito"]):
        for col, v in zip("TUVW", (row["start"], row["base"], row["rate"], row["over"]), strict=True):
            ws[f"{col}{2 + k}"] = v
    for col, name in zip(("Y", "Z", "AA", "AB"), ("Family lower", "Family upper", "Lower per child", "Upper per child"),
                         strict=True):
        put(ws, f"{col}1", name, bold=True)
    fam = rates["medicare_family"]
    ws["Y2"], ws["Z2"], ws["AA2"], ws["AB2"] = fam["lower"], fam["upper"], fam["lower_child"], fam["upper_child"]
    put(ws, "A30", "Copied from the ATO's pages by scripts/ato_rates.py. Rates sheet picks the tax year's block.")
    return ws, years


# ---- Rates: the sources and the active year's tables ------------------------------------

def rates_sheet(wb, rates, years):
    ws = wb.create_sheet("Rates")
    n = len(years)
    last = f"RateData!$N$2:$N${n + 1}"
    put(ws, "A1", "Tax rates from the ATO", bold=True)
    put(ws, "A2", "Fetched from ato.gov.au on")
    ws["B2"] = rates["fetched"]
    for i, (key, name) in enumerate((("resident", "Income tax rates"), ("help", "HELP repayment rates"),
                                     ("medicare", "Medicare levy thresholds"), ("medicare_family", "Medicare levy family thresholds"),
                                     ("lito", "Low income tax offset"))):
        ws[f"A{3 + i}"], ws[f"B{3 + i}"] = name, rates["sources"][key]

    # Which rates each year of the lease uses, from the lease start date (Inputs!B3).
    for col, name in zip("ABCDEF", ("Lease year", "Income tax year starts", "Income tax year", "Position",
                                    "Rates used", "Note"), strict=True):
        put(ws, f"{col}8", name, fill=HEAD, bold=True)
    for y in (1, 2, 3):
        r = 8 + y
        ws[f"A{r}"] = y
        ws[f"B{r}"] = f"=IF(MONTH(Inputs!$B$3)>6,YEAR(Inputs!$B$3),YEAR(Inputs!$B$3)-1)+{y - 1}"
        ws[f"C{r}"] = f'=B{r}&"-"&TEXT(MOD(B{r}+1,100),"00")'
        ws[f"D{r}"] = f"=IFERROR(MATCH(C{r},{last},0),IF(B{r}>VALUE(LEFT(INDEX({last},1),4)),1,COUNTA({last})))"
        ws[f"E{r}"] = f"=INDEX({last},D{r})"
        ws[f"F{r}"] = f'=IF(E{r}<>C{r},"The ATO has no "&C{r}&" rates yet: "&E{r}&" is used","")'

    for y in (1, 2, 3):
        first = TAX_FIRST[y]
        pos = f"$D${8 + y}"
        put(ws, f"A{first - 2}", f"Income tax, lease year {y}", bold=True)
        for col, name in zip("ABCDE", ("From (lookup)", "Base", "Marginal rate", "Over", "Bracket"), strict=True):
            put(ws, f"{col}{first - 1}", name, fill=HEAD, bold=True)
        for k in range(TAX_ROWS):
            r = first + k
            for col, src in zip("ABCD", "BCDE", strict=True):
                ws[f"{col}{r}"] = f"=INDEX(RateData!${src}$2:${src}${1 + n * TAX_ROWS},({pos}-1)*{TAX_ROWS}+{k + 1})"
            rate = f'SUBSTITUTE(TEXT(C{r},"0.0%"),".0%","%")'
            low = f"IF(A{r}=0,0,ROUND(A{r}+0.5,0))"
            if k + 1 < TAX_ROWS:
                ws[f"E{r}"] = (f'="$"&TEXT({low},"#,##0")&" - $"&TEXT(ROUND(A{r + 1}-0.5,0),"#,##0")'
                               f'&" ("&{rate}&")"')
            else:
                ws[f"E{r}"] = f'="$"&TEXT({low},"#,##0")&" and over ("&{rate}&")"'

        first = HELP_FIRST[y]
        put(ws, f"A{first - 2}", f"HELP repayment, lease year {y} (base + rate x income over a threshold, "
                                 "or rate x all income)", bold=True)
        for col, name in zip("ABCDE", ("From (lookup)", "Base", "Rate", "Over", "Rate on all income (1 = yes)"),
                             strict=True):
            put(ws, f"{col}{first - 1}", name, fill=HEAD, bold=True)
        for k in range(HELP_ROWS):
            r = first + k
            for col, src in zip("ABCDE", "HIJKL", strict=True):
                ws[f"{col}{r}"] = f"=INDEX(RateData!${src}$2:${src}${1 + n * HELP_ROWS},({pos}-1)*{HELP_ROWS}+{k + 1})"

    m = MEDICARE_AT
    put(ws, f"A{m}", "Medicare levy", bold=True)
    ws[f"A{m + 1}"], ws[f"B{m + 1}"] = "Lower threshold (single, not a pensioner)", "=RateData!Q2"
    ws[f"A{m + 2}"], ws[f"B{m + 2}"] = "Upper threshold", "=RateData!R2"
    put(ws, f"A{m + 3}", "Levy rate")
    put(ws, f"B{m + 3}", 0.02, INPUT)
    put(ws, f"A{m + 4}", "Phase-in rate: cents of levy for each $1 over the lower threshold")
    put(ws, f"B{m + 4}", 0.10, INPUT)
    ws[f"A{m + 5}"] = ('="The ATO publishes these thresholds for "&RateData!P2&" and the workbook uses them for '
                       'every year. Phase-in: the levy rises from nil at the lower threshold to the full rate at the upper."')

    ws[f"A{m + 6}"], ws[f"B{m + 6}"] = "Family lower threshold (no levy at or under it)", "=RateData!Y2"
    ws[f"A{m + 7}"], ws[f"B{m + 7}"] = "Family upper threshold", "=RateData!Z2"
    ws[f"A{m + 8}"], ws[f"B{m + 8}"] = "Lower threshold: increase for each dependent child", "=RateData!AA2"
    ws[f"A{m + 9}"], ws[f"B{m + 9}"] = "Upper threshold: increase for each dependent child", "=RateData!AB2"
    ws[f"A{m + 10}"] = ('="Family thresholds for "&RateData!P2&", for people without the seniors and pensioners tax offset. '
                        'Between the family thresholds the ATO reduces the levy; this workbook does not work that amount out."')

    put(ws, f"A{LITO_FIRST - 2}", "Low income tax offset", bold=True)
    for col, name in zip("ABCD", ("From (lookup)", "Base", "Rate", "Over"), strict=True):
        put(ws, f"{col}{LITO_FIRST - 1}", name, fill=HEAD, bold=True)
    for k in range(len(rates["lito"])):
        for col, src in zip("ABCD", "TUVW", strict=True):
            ws[f"{col}{LITO_FIRST + k}"] = f"=RateData!{src}{2 + k}"

    put(ws, f"A{SBITO_AT}", "Small business income tax offset (not read from the ATO: check the current figures)", bold=True)
    put(ws, f"A{SBITO_AT + 1}", "Rate")
    put(ws, f"B{SBITO_AT + 1}", 0.16, INPUT)
    put(ws, f"A{SBITO_AT + 2}", "Yearly cap")
    put(ws, f"B{SBITO_AT + 2}", 1000, INPUT)
    ws.column_dimensions["A"].width = 58
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["E"].width = 34
    return ws


# ---- Inputs ------------------------------------------------------------------------------

CARS = [  # row, label, car A, car B, number format
    (10, "Description", "Petrol wagon", "Electric SUV", None),
    (11, "FBT base value", 30000, 60000, "$#,##0"),
    (12, "Value at start of year 1", 27000, 52000, "$#,##0"),
    (13, "Exempt from FBT (eligible EV): 1 = yes", 0, 1, None),
    (14, "Statutory FBT rate", 0.20, 0.20, "0%"),
    (15, "Days held in the FBT year", 365, 365, None),
    (16, "Days in the FBT year", 365, 365, None),
    (17, "Km per year", 10000, 15000, "#,##0"),
    (18, "Fuel or energy per 100 km (L or kWh)", 6.5, 16, None),
    (19, "Price per L or kWh", 2.00, 0.25, "$#,##0.00"),
    (20, "Maintenance per year", 500, 300, "$#,##0"),
    (21, "Registration and CTP per year", 900, 900, "$#,##0"),
    (22, "Insurance per year", 1200, 1500, "$#,##0"),
    (23, "Depreciation rate (diminishing value)", 0.25, 0.25, "0%"),
    (24, "Depreciation cost limit (the ATO's car limit: check the figure for your car)", 69674, 69674, "$#,##0"),
]


def inputs_sheet(wb, rates, years):
    ws = wb.create_sheet("Inputs")
    labels = bracket_labels(rates["tax"][rates_year(start_year_of(DEFAULT_START), years)])
    put(ws, "A1", "Invented example figures. Blue cells are inputs.", bold=True)
    put(ws, "A3", "Lease start date (decides the income tax year for each year of the lease)")
    put(ws, "B3", DEFAULT_START, INPUT, fmt="d mmm yyyy")
    for row, label, value, fmt in ((4, "Lease term (years, 1-5)", 2, None), (5, "Pays per year", 26, None),
                                   (6, "Provider admin fee per pay", 1, "$#,##0.00"),
                                   (7, "Other pre-tax package fees per year", 400, "$#,##0")):
        put(ws, f"A{row}", label)
        put(ws, f"B{row}", value, INPUT, fmt=fmt)

    put(ws, "A8", "Dependent children (raises the family Medicare thresholds)")
    put(ws, "B8", 0, INPUT)

    put(ws, "A9", "Cars", bold=True)
    put(ws, "B9", "Car A", bold=True)
    put(ws, "C9", "Car B", bold=True)
    for row, label, a, b, fmt in CARS:
        put(ws, f"A{row}", label)
        put(ws, f"B{row}", a, INPUT, fmt=fmt)
        put(ws, f"C{row}", b, INPUT, fmt=fmt)

    put(ws, "A27", "People", bold=True)
    put(ws, "B27", "Person A (packages the cars)", bold=True)
    put(ws, "C27", "Person B (leases the cars through their own business)", bold=True)
    put(ws, "A28", "Tax bracket (taxable income, the lease's first year)")
    put(ws, "A29", "Gross salary (optional: leave blank to use the middle of the bracket)")
    put(ws, "A30", "Income used")
    put(ws, "A31", "Other pre-tax deductions")
    put(ws, "A32", "HELP debt")
    put(ws, "A33", "Medicare levy")
    put(ws, "A34", "Share of the levy payable (before the partner rule, which Scenario applies)")
    put(ws, "A35", "Check")
    put(ws, "A36", "Bracket position")
    put(ws, "A37", "Dependants (for the exemption rule)")
    put(ws, "A38", "Family agreement with the partner (both exempt, children shared)")
    put(ws, "A39", "Check")
    for col, bracket, debt in (("B", labels[2], 0), ("C", labels[1], 20000)):
        put(ws, f"{col}28", bracket, INPUT)
        put(ws, f"{col}29", None, INPUT, fmt="$#,##0")
        ws[f"{col}30"] = (f"=IF({col}29<>\"\",{col}29,IF({col}36={TAX_ROWS},INDEX(Rates!$A$17:$A$21,{TAX_ROWS})+10000.5,"
                          f"ROUND((INDEX(Rates!$A$17:$A$21,{col}36)+INDEX(Rates!$A$17:$A$21,{col}36+1))/2,0)))")
        put(ws, f"{col}31", 0, INPUT, fmt="$#,##0")
        put(ws, f"{col}32", debt, INPUT, fmt="$#,##0")
        put(ws, f"{col}33", "Not exempt", INPUT)
        ws[f"{col}34"] = f'=IF({col}33="Exempt",0,IF({col}33="Half exempt",0.5,1))'
        ws[f"{col}35"] = (f'=IF(ISNA(MATCH({col}28,Rates!$E$17:$E$21,0)),'
                          f'"Pick a bracket from the list for the first year","")')
        ws[f"{col}36"] = f"=IFERROR(MATCH({col}28,Rates!$E$17:$E$21,0),3)"
        put(ws, f"{col}37", "Partner only", INPUT)
        put(ws, f"{col}38", "None", INPUT)
        ws[f"{col}39"] = ('=IF(AND(B38="Claim full",C38="Claim full"),"A family agreement gives one of you the full '
                          'exemption and the other the half: choose Claim half for one","")')
        ws[f"{col}30"].number_format = MONEY

    put(ws, "A42", "Lease terms (from your quote)", bold=True)
    for col, name in zip("ABC", ("Term (years)", "Lease rate: yearly payments / value financed (EXAMPLE)",
                                 "ATO minimum residual"), strict=True):
        put(ws, f"{col}43", name, fill=HEAD, bold=True)
    for k, (term, rate) in enumerate(((1, 0.50), (2, 0.36), (3, 0.30), (4, 0.26), (5, 0.23))):
        r = 44 + k
        put(ws, f"A{r}", term)
        put(ws, f"B{r}", rate, INPUT)
        ws[f"C{r}"] = f"=75%-(75%/8)*A{r}"
        ws[f"C{r}"].number_format = "0.000%"

    for cell, formula in (("B28", "=Rates!$E$17:$E$21"), ("C28", "=Rates!$E$17:$E$21"),
                          ("B33", f'"{MEDICARE_CHOICES}"'), ("C33", f'"{MEDICARE_CHOICES}"'),
                          ("B37", f'"{DEPENDANTS}"'), ("C37", f'"{DEPENDANTS}"'),
                          ("B38", f'"{AGREEMENT}"'), ("C38", f'"{AGREEMENT}"')):
        dv = DataValidation(type="list", formula1=formula, allow_blank=False)
        dv.showErrorMessage = True
        ws.add_data_validation(dv)
        dv.add(cell)
    ws.column_dimensions["A"].width = 70
    for col in "BC":
        ws.column_dimensions[col].width = 34
    return ws


# ---- Scenario ----------------------------------------------------------------------------

def tax_range(y):
    return f"Rates!$A${TAX_FIRST[y]}:$D${TAX_FIRST[y] + TAX_ROWS - 1}"


def help_range(y):
    return f"Rates!$A${HELP_FIRST[y]}:$E${HELP_FIRST[y] + HELP_ROWS - 1}"


LITO = f"Rates!$A${LITO_FIRST}:$D${LITO_FIRST + 3}"
M = MEDICARE_AT


def tax(x, y):
    t = tax_range(y)
    return f"(VLOOKUP({x},{t},2,TRUE)+VLOOKUP({x},{t},3,TRUE)*({x}-VLOOKUP({x},{t},4,TRUE)))"


def share(ic, partner_ic, partner_taxable):
    """The part of the levy a person pays, by the ATO's table. The partner counts if they are in an exemption
    category (any choice but Not exempt) or have to pay the levy (taxable income over the lower threshold, and
    not exempt, from Inputs row 34); shared children need the partner to pay (or a family agreement)."""
    pays = f"AND({partner_taxable}>Rates!$B${M + 1},Inputs!{partner_ic}34>0)"
    rule = (f'IF(Inputs!{ic}37="No dependants",0,'
            f'IF(Inputs!{ic}37="Partner and other dependants",0.5,'
            f'IF(Inputs!{ic}37="Partner and shared children",IF(OR({pays},Inputs!{ic}38="Claim full"),0,0.5),'
            f'IF(OR(Inputs!{partner_ic}33<>"Not exempt",{pays}),0,0.5))))')
    return (f'IF(Inputs!{ic}33="Exempt",0,IF(Inputs!{ic}33="Half exempt",0.5,'
            f'IF(Inputs!{ic}33="{PARTNER_RULE}",{rule},1)))')


def medicare(x, share_expr, family):
    """The levy on x. Nil at or under the family threshold (more with dependent children) for someone whose own
    income is over the single threshold; the reduction between the family thresholds is not worked out."""
    return (f"IF(AND({x}>Rates!$B${M + 2},{family}<=Rates!$B${M + 6}+Inputs!$B$8*Rates!$B${M + 8}),0,"
            f"IF({x}<=Rates!$B${M + 1},0,IF({x}<=Rates!$B${M + 2},({x}-Rates!$B${M + 1})*Rates!$B${M + 4},"
            f"{x}*Rates!$B${M + 3}))*{share_expr})")


def family_flag(a, b):
    f = f"{a}+{b}"
    return (f'=IF(AND(OR({a}>Rates!$B${M + 2},{b}>Rates!$B${M + 2}),{f}>Rates!$B${M + 6}+Inputs!$B$8*Rates!$B${M + 8},'
            f'{f}<=Rates!$B${M + 7}+Inputs!$B$8*Rates!$B${M + 9}),'
            f'"Family reduction may apply: the levy is not reduced here","")')


def lito(x, tax_cell):
    return (f"-MIN({tax_cell},MAX(0,VLOOKUP({x},{LITO},2,TRUE)-VLOOKUP({x},{LITO},3,TRUE)"
            f"*({x}-VLOOKUP({x},{LITO},4,TRUE))))")


def help_(x, debt, y):
    h = help_range(y)
    return (f"MIN({debt},IF({x}<=0,0,VLOOKUP({x},{h},2,TRUE)+VLOOKUP({x},{h},3,TRUE)"
            f"*IF(VLOOKUP({x},{h},5,TRUE)=1,{x},{x}-VLOOKUP({x},{h},4,TRUE))))")


LABELS = {
    1: "FBT year", 2: "Car / person",
    3: "Value at start of the year", 4: "Fuel or energy", 5: "Maintenance", 6: "Registration and CTP",
    7: "Insurance", 8: "Running costs (inc GST)", 9: "Lease payments", 10: "FBT taxable value",
    11: "Depreciation (on the car's cost, up to the car limit)",
    13: "WITHOUT THE LEASE", 14: "Taxable income", 15: "Income tax", 16: "Medicare levy",
    17: "Low income tax offset", 18: "HELP repayment", 19: "Net income",
    20: "Running costs paid after tax", 21: "Cash in hand",
    23: "WITH THE LEASE", 24: "Gross salary", 25: "Lease payments received (business income)",
    26: "Other pre-tax package fees", 27: "Total income",
    28: "Pre-tax package amount (lease + running ex GST - post-tax contribution + admin fees)",
    29: "GST on the post-tax contribution", 30: "Depreciation claimed",
    31: "Small business income (lease payments - depreciation)", 32: "Taxable income",
    33: "Income tax", 34: "Medicare levy", 35: "Low income tax offset", 36: "HELP repayment",
    37: "Small business income tax offset", 38: "Post-tax contribution (employee contribution method)",
    39: "GST reimbursed on running costs", 40: "Cash in hand",
    42: "NET BENEFIT OF THE LEASE (cash in hand with - without)",
    45: "Family Medicare check, without the lease", 46: "Family Medicare check, with the lease",
}


def scenario_sheet(wb):
    sc = wb.create_sheet("Scenario")
    sc.column_dimensions["A"].width = 62
    for r, text in LABELS.items():
        put(sc, f"A{r}", text, bold=r in (1, 2, 13, 21, 23, 40, 42))
    for c0, year in ((2, 1), (5, 2), (8, 3)):
        cols = [chr(ord("A") + c0 - 1 + i) for i in range(3)]
        a, b, tot = cols
        put(sc, f"{a}1", f"Year {year}", bold=True)
        sc[f"{a}2"], sc[f"{b}2"] = "=Inputs!B9", "=Inputs!C9"
        put(sc, f"{tot}2", "Total", bold=True)
        for col, ic in ((a, "B"), (b, "C")):
            prev = chr(ord(col) - 3)
            sc[f"{col}3"] = f"=Inputs!{ic}12" if year == 1 else f"={prev}3-{prev}11"
            sc[f"{col}4"] = f"=Inputs!{ic}17/100*Inputs!{ic}18*Inputs!{ic}19"
            sc[f"{col}5"], sc[f"{col}6"], sc[f"{col}7"] = (f"=Inputs!{ic}20", f"=Inputs!{ic}21", f"=Inputs!{ic}22")
            sc[f"{col}8"] = f"=SUM({col}4:{col}7)"
            sc[f"{col}9"] = f"={col}3*VLOOKUP(Inputs!$B$4,Inputs!$A$44:$B$48,2,FALSE)"
            sc[f"{col}10"] = f"=Inputs!{ic}11*Inputs!{ic}14*Inputs!{ic}15/Inputs!{ic}16*(1-Inputs!{ic}13)"
            sc[f"{col}11"] = f"=MIN({col}3,Inputs!{ic}24)*Inputs!{ic}23"
        for r in range(3, 12):
            sc[f"{tot}{r}"] = f"=SUM({a}{r}:{b}{r})"
        partner = {a: (b, "C"), b: (a, "B")}       # each person's partner: column and Inputs column
        for col, ic in ((a, "B"), (b, "C")):       # without the lease: column a is Person A, b is Person B
            sc[f"{col}14"] = f"=Inputs!{ic}30-Inputs!{ic}31"
            sc[f"{col}15"] = "=" + tax(f"{col}14", year)
            sc[f"{col}16"] = "=" + medicare(f"{col}14", share(ic, partner[col][1], f"{partner[col][0]}14"),
                                            f"{a}14+{b}14")
            sc[f"{col}17"] = "=" + lito(f"{col}14", f"{col}15")
            sc[f"{col}18"] = "=" + help_(f"{col}14", f"Inputs!{ic}32", year)
            sc[f"{col}19"] = f"={col}14-SUM({col}15:{col}18)"
        sc[f"{a}20"] = f"={tot}8"
        sc[f"{a}21"] = f"={a}19-{a}20"
        sc[f"{b}21"] = f"={b}19"
        sc[f"{tot}21"] = f"=SUM({a}21:{b}21)"
        sc[f"{a}24"], sc[f"{b}24"] = "=Inputs!B30", "=Inputs!C30"      # with the lease
        sc[f"{b}25"] = f"={tot}9"
        sc[f"{a}26"] = "=Inputs!B7"
        sc[f"{a}27"] = f"={a}24-{a}26-Inputs!B31"
        sc[f"{b}27"] = f"={b}24+{b}25-Inputs!C31"
        sc[f"{a}28"] = f"={tot}9+{tot}8/1.1-{tot}10+Inputs!B6*Inputs!B5"
        sc[f"{a}29"] = f"={a}38/11"
        sc[f"{b}30"] = f"={tot}11"
        sc[f"{b}31"] = f"={b}25-{b}30"
        sc[f"{a}32"] = f"={a}27-{a}28-{a}29"
        sc[f"{b}32"] = f"={b}27-{b}30"
        for col, ic in ((a, "B"), (b, "C")):
            sc[f"{col}33"] = "=" + tax(f"{col}32", year)
            sc[f"{col}34"] = "=" + medicare(f"{col}32", share(ic, partner[col][1], f"{partner[col][0]}32"),
                                            f"{a}32+{b}32")
            sc[f"{col}35"] = "=" + lito(f"{col}32", f"{col}33")
            sc[f"{col}36"] = "=" + help_(f"{col}32", f"Inputs!{ic}32", year)
        sc[f"{b}37"] = f"=-MIN(IF({b}32>0,MAX(0,{b}31)/{b}32,0)*{b}33*Rates!$B${SBITO_AT + 1},Rates!$B${SBITO_AT + 2})"
        sc[f"{a}38"] = f"={tot}10"
        sc[f"{a}39"] = f"={tot}8/11"
        sc[f"{a}40"] = f"={a}32-SUM({a}33:{a}36)-{a}38+{a}39"
        sc[f"{b}40"] = f"={b}27-SUM({b}33:{b}37)"
        sc[f"{tot}40"] = f"=SUM({a}40:{b}40)"
        sc[f"{tot}42"] = f"={tot}40-{tot}21"
        sc[f"{a}45"] = family_flag(f"{a}14", f"{b}14")
        sc[f"{a}46"] = family_flag(f"{a}32", f"{b}32")
        for r in range(3, 43):
            for col in cols:
                sc[f"{col}{r}"].number_format = MONEY
        for col in (a, b):
            sc.column_dimensions[col].width = 16
        sc.column_dimensions[tot].width = 14
        sc[f"{tot}42"].font = BOLD
    for r in (1, 2, 13, 23, 42):
        for c in sc[r]:
            c.fill = HEAD
    sc["A44"] = ('="Income tax and HELP rates from the ATO: year 1 uses "&Rates!E9&", year 2 "&Rates!E10&", year 3 "'
                 '&Rates!E11&", from the lease start date (see the Rates sheet)."')
    return sc


def build(rates, out):
    wb = Workbook()
    wb.remove(wb.active)
    scenario_sheet(wb)
    _, years = rate_data(wb, rates)
    inputs_sheet(wb, rates, years)
    rates_sheet(wb, rates, years)
    wb._sheets = [wb[n] for n in ("Scenario", "Inputs", "Rates", "RateData")]
    wb.active = 0
    wb.properties.creator = "GarageBooks"
    wb.properties.lastModifiedBy = "GarageBooks"
    wb.properties.title = "Novated lease what-if: example"
    wb.save(out)


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rates", help="rates saved by ato_rates.py --out; omit to read the ATO's pages now")
    ap.add_argument("--out", default=str(root / "docs" / "assets" / "lease-calculator-example.xlsx"))
    args = ap.parse_args(argv)
    if args.rates:
        rates = json.loads(Path(args.rates).read_text(encoding="utf-8"))
    else:
        import ato_rates
        try:
            rates = ato_rates.fetch_all()
        except ato_rates.RatesError as err:
            print(f"build_lease_workbook: {err}", file=sys.stderr)
            return 1
    build(rates, args.out)
    print(f"wrote {args.out} with rates fetched {rates['fetched']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
