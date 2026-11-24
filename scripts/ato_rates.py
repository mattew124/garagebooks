#!/usr/bin/env python3
"""Read Australian tax rates from the ATO's own pages, for the lease what-if workbook.

    scripts/ato_rates.py [--out rates.json]

Fetches the resident income tax rates, the study and training loan (HELP) repayment rates, the
Medicare levy low-income and family thresholds and the low income tax offset, and prints them as JSON. It
reads only https://www.ato.gov.au and writes nothing but the file you name. The reading itself is
garagebooks/ato.py, which the app's scheduled job shares.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from garagebooks.ato import RatesError, fetch_all  # noqa: E402 - the package is found from the repository root


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="write the JSON here instead of printing it")
    args = ap.parse_args(argv)
    try:
        data = fetch_all()
    except RatesError as err:
        print(f"ato_rates: {err}", file=sys.stderr)
        return 1
    blob = json.dumps(data, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(blob)
    else:
        sys.stdout.write(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
