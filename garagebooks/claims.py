"""Claims: sending an expense and its invoice to a claims connector, a small service that knows how to
claim it from the owner's salary packaging provider (protocol v1: docs/claims-connector.md).

Nothing here touches the database or Flask. It is the client for the connector, and the rules for which
expenses can be claimed. The whole feature is off unless CLAIMS_CONNECTOR_URL is set.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

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
# The signed token (docs/claims-connector.md, "Authorising requests"): short-lived, one per request.
TOKEN_ISSUER, TOKEN_AUDIENCE, TOKEN_LIFETIME = "garagebooks", "claims-connector", 60
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


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def thumbprint(public_key):
    """An Ed25519 public key's RFC 7638 JWK thumbprint: the `kid` of the tokens it verifies, which the
    connector works out for itself from its copy of the key."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64url(hashlib.sha256(b'{"crv":"Ed25519","kty":"OKP","x":"' + b64url(raw).encode() + b'"}').digest())


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
    """The client. `url` has no trailing /v1. With `key_file` (an Ed25519 private key in PEM form, read at each
    request so it can be rotated without a restart) every request carries a fresh signed token. Otherwise
    (deprecated) it sends the shared `token`, or the one in the file `token_file`."""

    def __init__(self, url, token=None, token_file=None, timeout=TIMEOUT_SECONDS, key_file=None):
        self.url = url.rstrip("/")
        self.token, self.token_file, self.key_file, self.timeout = token, token_file, key_file, timeout
        self.kid = None          # of the key last used, for the message on a refusal

    def _signed(self):
        """A new token, valid for a minute, signed with the key file's key."""
        try:
            with open(self.key_file, "rb") as handle:
                pem = handle.read()
        except OSError as exc:
            raise ConnectorError("The claims connector's signing key file can't be read. "
                                 "Check CLAIMS_CONNECTOR_KEY_FILE.") from exc
        not_ed25519 = ConnectorError("The claims connector's signing key file isn't an unencrypted Ed25519 "
                                     "private key in PEM form. Check CLAIMS_CONNECTOR_KEY_FILE.")
        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except (ValueError, TypeError) as exc:
            raise not_ed25519 from exc
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise not_ed25519
        self.kid = thumbprint(key.public_key())
        now = int(time.time())
        return jwt.encode({"iss": TOKEN_ISSUER, "aud": TOKEN_AUDIENCE, "iat": now, "exp": now + TOKEN_LIFETIME,
                           "jti": secrets.token_urlsafe(16)}, key, algorithm="EdDSA", headers={"kid": self.kid})

    def _token(self):
        if self.key_file:
            return self._signed()
        if self.token_file:
            try:
                with open(self.token_file, encoding="utf-8") as handle:
                    return handle.read().strip()
            except OSError as exc:
                raise ConnectorError("The claims connector's token file can't be read. "
                                     "Check CLAIMS_CONNECTOR_TOKEN_FILE.") from exc
        return (self.token or "").strip()

    def _refused(self):
        if self.key_file:
            return ConnectorError(
                "The claims connector refused the signed token. Check that CLAIMS_CONNECTOR_KEY_FILE is the key "
                f"whose public key the connector lists (key id {self.kid}), and that the clocks of both machines "
                "are right.")
        return ConnectorError("The claims connector refused the token. Check CLAIMS_CONNECTOR_TOKEN.")

    def call(self, method, path, body=None, timeout=None):
        """-> Reply. A connector's own error answers (4xx, 5xx with a JSON body) are replies, not exceptions."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # nosec B310 - the URL is the installer's own CLAIMS_CONNECTOR_URL, http(s) only
            f"{self.url}/v1/{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json",
                     **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:  # nosec B310 - see above
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
                raise self._refused()
            raise ConnectorError(f"The claims connector answered {status} with something other than JSON.")
        if status in (401, 403):
            raise self._refused()
        return Reply(status, parsed)

    def health(self, timeout=None):
        return self.call("GET", "health", timeout=timeout)

    def transactions(self, cars):
        """The provider's transaction history (the connector chooses the period), with each row's car given as
        one of `cars` ([{id, name, make, model}]) where the connector can tell."""
        return self.call("POST", "transactions", {"cars": list(cars)})

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


warned_legacy = []   # the deprecation is logged once per process


def from_env(environ=os.environ):
    """The connector the installer configured, or None (the feature is then invisible)."""
    url = (environ.get("CLAIMS_CONNECTOR_URL") or "").strip()
    if not url:
        return None
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise RuntimeError("CLAIMS_CONNECTOR_URL must be an http:// or https:// address, such as http://host:8765")
    key_file = (environ.get("CLAIMS_CONNECTOR_KEY_FILE") or "").strip() or None
    token_file = (environ.get("CLAIMS_CONNECTOR_TOKEN_FILE") or "").strip() or None
    token = environ.get("CLAIMS_CONNECTOR_TOKEN")
    if key_file:
        if token or token_file:
            log.warning("CLAIMS_CONNECTOR_KEY_FILE is set, so CLAIMS_CONNECTOR_TOKEN and CLAIMS_CONNECTOR_TOKEN_FILE "
                        "are ignored. Remove them.")
        return Connector(url, key_file=key_file)
    if (token or "").strip() or token_file:
        if not warned_legacy:
            warned_legacy.append(True)
            log.warning("CLAIMS_CONNECTOR_TOKEN and CLAIMS_CONNECTOR_TOKEN_FILE are deprecated: switch to "
                        "CLAIMS_CONNECTOR_KEY_FILE, which signs a fresh token for every request.")
    return Connector(url, token=token, token_file=token_file)


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


# ---- the claims page: where each expense stands, and the provider's transaction history ----------------------

TRANSACTION_KINDS = ("claim_payment", "per_km_charging", "other")
MAX_TRANSACTIONS = 20000


def block_reason(pool, amount, has_lease):
    """Why an expense can't be claimed (a phrase), or None. Whether it was claimed already is not asked here."""
    if pool not in CLAIMABLE_POOLS:
        return "its category isn't one of the provider's claimable budget pools"
    if amount is None or amount <= 0:
        return "only money spent can be claimed, not a refund"
    if not has_lease:
        return "no lease for this car covers its date"
    return None


def expense_state(pool, amount, has_lease, claimed, provider_matched):
    """-> (state, reason): 'claimed' (claimed from here, or the provider's claim history has it), 'ready' (can be
    claimed now) or 'blocked' (with the reason)."""
    if claimed or provider_matched:
        return "claimed", None
    reason = block_reason(pool, amount, has_lease)
    return ("blocked", reason) if reason else ("ready", None)


def reimbursement(expected, received):
    """Has the provider paid a claim in full? -> (state, difference): 'paid', 'part' (received something, less than
    expected), 'unpaid' (nothing yet) or 'over' (more than expected). A cent either way is rounding."""
    expected, received = Decimal(expected or 0), Decimal(received or 0)
    gap = (expected - received).quantize(Decimal("0.01"))
    if received <= 0:
        return "unpaid", expected
    if gap > Decimal("0.01"):
        return "part", gap
    if gap < Decimal("-0.01"):
        return "over", -gap
    return "paid", Decimal(0)


def clean_transaction(item, car_ids):
    """One row of the connector's transaction list as the cache keeps it, or None if it is unusable.
    `car_ids` is the set of car ids the app has: a car the app doesn't know is dropped to None."""
    if not isinstance(item, dict):
        return None
    provider_id = text(item.get("provider_id"), 200)
    amount = number(item.get("amount"))
    try:
        day = date.fromisoformat(str(item.get("date"))[:10])
    except ValueError:
        return None
    if not provider_id or amount is None:
        return None
    car_id = item.get("car_id")
    return {"provider_id": provider_id, "occurred_on": day, "amount": amount, "gst": number(item.get("gst")),
            "type": text(item.get("type"), 200), "status": text(item.get("status"), 200),
            "description": text(item.get("description"), 500), "payee": text(item.get("payee"), 300),
            "kind": item.get("kind") if item.get("kind") in TRANSACTION_KINDS else "other",
            "car_id": car_id if isinstance(car_id, int) and not isinstance(car_id, bool) and car_id in car_ids else None,
            "claim_provider_id": text(item.get("claim_provider_id"), 200), "reference": text(item.get("reference"), 200)}


def match_by_figures(lines, expenses):
    """Pair each claim line with the one expense it must be: the same car (or, if the connector couldn't say,
    any car), local day and amount, when exactly one line and exactly one expense fit. Anything ambiguous is
    left alone. `lines`, `expenses`: [(id, car_id, day, amount)] -> {line id: expense id}."""
    by_expense = defaultdict(list)
    for eid, car, day, amount in expenses:
        by_expense[(car, day, abs(amount))].append(eid)
        by_expense[(None, day, abs(amount))].append(eid)
    by_payment = defaultdict(list)
    for tid, car, day, amount in lines:
        by_payment[(car, day, abs(amount))].append(tid)
    out = {}
    for (car, day, amount), tids in by_payment.items():
        eids = by_expense.get((car, day, amount), [])
        if len(tids) == 1 and len(eids) == 1:
            out[tids[0]] = eids[0]
    return out


def clean_claim_line(item, car_ids):
    """One claim line of the connector's claim history as the cache keeps it, or None if it is unusable."""
    if not isinstance(item, dict):
        return None
    provider_id = text(item.get("provider_id"), 200)
    amount = number(item.get("amount"))
    try:
        day = date.fromisoformat(str(item.get("date"))[:10])
    except ValueError:
        return None
    if not provider_id or amount is None:
        return None
    try:
        paid = date.fromisoformat(str(item.get("paid_date"))[:10]) if item.get("paid_date") else None
    except ValueError:
        paid = None
    car_id = item.get("car_id")
    return {"provider_id": provider_id, "line_id": text(item.get("line_id"), 200) or "", "reference": text(item.get("reference"), 200),
            "occurred_on": day, "amount": amount, "approved_amount": number(item.get("approved_amount")),
            "status": text(item.get("status"), 200), "paid_date": paid, "description": text(item.get("description"), 500),
            "car_id": car_id if isinstance(car_id, int) and not isinstance(car_id, bool) and car_id in car_ids else None}
