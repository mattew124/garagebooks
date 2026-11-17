"""Claims: sending an expense and its invoice to a claims connector, a small service that knows how to
claim it from the owner's salary packaging provider (protocol v1: docs/claims-connector.md).

Nothing here touches the database or Flask. It is the client for the connector, and the rules for which
expenses can be claimed. The whole feature is off unless CLAIMS_CONNECTOR_URL is set.
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

log = logging.getLogger("garagebooks.claims")

# The provider's budget pools that are claimed back by hand. The lease payment is paid by the provider.
CLAIMABLE_POOLS = ("fuel_charging", "tyres", "maintenance", "insurance", "registration", "other")
MAX_INVOICE_BYTES = 10 * 1024 * 1024
# A connector drives a browser, and a call can take a minute. Gunicorn's own timeout (gunicorn.conf.py)
# is longer, so the page shows this module's message rather than a killed worker.
TIMEOUT_SECONDS = 150
# What the connector may ask for in a 422, and how each is typed. Anything else is ignored.
FIELD_KEYS = ("policy_number", "start", "end", "description", "provider", "invoice_date")
FIELD_TYPES = ("date", "text")
SIGNIN_STATES = ("idle", "starting", "code_needed", "checking", "signed_in", "failed")
FIELD_LIMIT = 500
MAX_MONEY = Decimal(10) ** 10   # what numeric(12, 2) can hold, with room: a bigger figure is not a money value


class ConnectorError(Exception):
    """The connector couldn't be reached, or answered with something that isn't protocol v1."""

    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class Reply:
    status: int
    body: dict

    @property
    def ok(self):
        return self.status == 200 and self.body.get("ok") is True

    @property
    def error(self):
        return text(self.body.get("error")) or f"The connector answered {self.status}."

    @property
    def needs_signin(self):
        return self.status == 409 and self.body.get("needs") == "signin"

    @property
    def needs_fields(self):
        """-> [{key, label, type}] the connector wants filled in, or [] (a 422 without usable fields too)."""
        if self.status != 422:
            return []
        out = []
        for item in self.body.get("needs_fields") or []:
            if isinstance(item, dict) and item.get("key") in FIELD_KEYS:
                out.append({"key": item["key"], "label": text(item.get("label")) or item["key"],
                            "type": item.get("type") if item.get("type") in FIELD_TYPES else "text"})
        return out


def text(value, limit=2000):
    """Whatever the connector sent as a plain, bounded string (None for anything else)."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def number(value):
    """A money value from the connector as a Decimal to the cent, or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = Decimal(str(value))
        if not out.is_finite() or abs(out) >= MAX_MONEY:
            return None
        return out.quantize(Decimal("0.01"))
    except ArithmeticError:
        return None


class Connector:
    """The client. `url` has no trailing /v1. The token is read from `token` or, at each request, from
    the file `token_file` (so it can be rotated without a restart)."""

    def __init__(self, url, token=None, token_file=None, timeout=TIMEOUT_SECONDS):
        self.url = url.rstrip("/")
        self.token, self.token_file, self.timeout = token, token_file, timeout

    def _token(self):
        if self.token_file:
            try:
                with open(self.token_file, encoding="utf-8") as handle:
                    return handle.read().strip()
            except OSError as exc:
                raise ConnectorError("The claims connector's token file can't be read. "
                                     "Check CLAIMS_CONNECTOR_TOKEN_FILE.") from exc
        return (self.token or "").strip()

    def call(self, method, path, body=None):
        """-> Reply. A connector's own error answers (4xx, 5xx with a JSON body) are replies, not exceptions."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # nosec B310 - the URL is the installer's own CLAIMS_CONNECTOR_URL, http(s) only
            f"{self.url}/v1/{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json",
                     **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # nosec B310 - see above
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except TimeoutError as exc:
            raise ConnectorError("The claims connector took too long to answer. The claim may still have "
                                 "gone through at the provider: check there before trying again.") from exc
        except (urllib.error.URLError, OSError) as exc:
            log.warning("claims connector unreachable: %s", getattr(exc, "reason", exc))
            raise ConnectorError("The claims connector can't be reached. Check that it is running and that "
                                 "CLAIMS_CONNECTOR_URL is right.") from exc
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            if status in (401, 403):
                raise ConnectorError("The claims connector refused the token. Check CLAIMS_CONNECTOR_TOKEN.")
            raise ConnectorError(f"The claims connector answered {status} with something other than JSON.")
        if status in (401, 403):
            raise ConnectorError("The claims connector refused the token. Check CLAIMS_CONNECTOR_TOKEN.")
        return Reply(status, parsed)

    def health(self):
        return self.call("GET", "health")

    def preview(self, expense, invoice):
        return self.call("POST", "preview", {"expense": expense, "invoice": invoice})

    def submit(self, expense, invoice):
        return self.call("POST", "submit", {"expense": expense, "invoice": invoice, "confirm": True})

    def status(self, provider_ids):
        return self.call("POST", "status", {"provider_ids": list(provider_ids)})

    def signin(self):
        return self.call("POST", "signin", {})

    def signin_code(self, code):
        return self.call("POST", "signin/code", {"code": code})


def from_env(environ=os.environ):
    """The connector the installer configured, or None (the feature is then invisible)."""
    url = (environ.get("CLAIMS_CONNECTOR_URL") or "").strip()
    if not url:
        return None
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise RuntimeError("CLAIMS_CONNECTOR_URL must be an http:// or https:// address, such as http://host:8765")
    token_file = (environ.get("CLAIMS_CONNECTOR_TOKEN_FILE") or "").strip() or None
    return Connector(url, token=environ.get("CLAIMS_CONNECTOR_TOKEN"), token_file=token_file)


def covered(spans, car_id, day):
    """Does a lease of this car cover the day? `spans` maps car id to [(start, end)] (both ends included)."""
    return any(start <= day <= end for start, end in spans.get(car_id, ()))


def claimable(pool, amount, has_lease, claimed):
    """May this expense be claimed? A claimable pool, money spent (not a refund), a lease covering the date,
    and not claimed already."""
    return pool in CLAIMABLE_POOLS and amount is not None and amount > 0 and has_lease and not claimed


def expense_payload(row, local_day, fields=None):
    """The EXPENSE object of the protocol, from an expense row (db.get_expense). `local_day` is the date
    in APP_TZ. Only fields the owner or the connector asked for are sent."""
    return {"id": row["id"],
            "car": {"id": row["car_id"], "name": row["car_name"], "make": row.get("car_make"),
                    "model": row.get("car_model")},
            "pool": row["budget_pool"], "category": row["category"], "date": local_day.isoformat(),
            "amount": float(row["amount"]), "vendor": row["vendor"], "notes": row["notes"],
            "fields": {k: v for k, v in (fields or {}).items() if v}}


def invoice_payload(name, content, content_type="application/pdf"):
    return {"name": name, "content_type": content_type, "data_base64": base64.b64encode(content).decode("ascii")}


def clean_fields(form):
    """What the claim page's form sent for the connector's fields -> (fields, errors). Known keys only,
    trimmed and bounded. GST is a money value, or blank to leave it to the connector."""
    out, errors = {}, []
    for key in FIELD_KEYS:
        value = (form.get(f"field_{key}") or "").strip()[:FIELD_LIMIT]
        if value:
            out[key] = value
    gst = (form.get("field_gst") or "").strip().replace("$", "").replace(",", "")
    if gst:
        amount = number(gst)
        if amount is None or amount < 0:
            errors.append("GST must be an amount such as 12.34, or left empty.")
        else:
            out["gst"] = str(amount)
    return out, errors
