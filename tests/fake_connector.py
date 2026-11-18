"""A stand-in claims connector (protocol v1, docs/claims-connector.md), for the tests and the dev stack.

It never talks to a provider: it answers from memory with invented values. `Fake.start()` runs it in a
thread on 127.0.0.1 for the unit tests; `python3 fake_connector.py [port]` runs it as the dev stack's
`dev-claims-connector` service, where the browser tests change its behaviour with POST /_fake/config
(the same dict as `Fake.config`) and POST /_fake/reset.
"""

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "fake-connector-token"
PROVIDER = "Example Salary Packaging"


def defaults():
    return {
        "signed_in": True,
        # fields the preview insists on (a 422) until expense.fields has them: [{"key", "label", "type"}]
        "needs_fields": [],
        "warnings": [],
        # {"preview"|"submit"|"status": (http status, body)} replaces that answer
        "override": {},
        "code": "123456",
        "signin": {"state": "idle"},
        "statuses": {},          # provider_id -> a status entry to answer with
        "transactions": [],      # what POST /v1/transactions answers with (rows as in docs/claims-connector.md)
        "claims": [],            # ... and its claim history (one row per claim line)
    }


class Fake:
    def __init__(self, token=TOKEN):
        self.token = token
        self.config = defaults()
        self.calls = []          # (method, path, authorization header, parsed body)
        self.submitted = {}      # expense id -> provider id
        self.server = None

    def reset(self):
        self.config = defaults()
        self.calls.clear()
        self.submitted.clear()

    # ---- the protocol ---------------------------------------------------------------------------

    def handle(self, method, path, auth, body):
        self.calls.append((method, path, auth, body))
        if auth != f"Bearer {self.token}":
            return 401, {"ok": False, "error": "bad token"}
        cfg = self.config
        if (method, path) == ("GET", "/v1/health"):
            return 200, {"ok": True, "provider": PROVIDER, "session_saved": cfg["signed_in"],
                         "signin": cfg["signin"]}
        if (method, path) == ("POST", "/v1/signin"):
            cfg["signin"] = {"state": "code_needed", "message": "Enter the code the provider sent you."}
            return 200, {"ok": True, "state": "starting"}
        if (method, path) == ("POST", "/v1/signin/code"):
            if body.get("code") == cfg["code"]:
                cfg["signed_in"], cfg["signin"] = True, {"state": "signed_in"}
            else:
                cfg["signin"] = {"state": "failed", "error": "That code was not accepted."}
            return 200, {"ok": True, "state": "checking"}
        if (method, path) == ("POST", "/v1/status"):
            if "status" in cfg["override"]:
                return cfg["override"]["status"]
            claims = [cfg["statuses"].get(i) or {"provider_id": i, "status": None, "error": "not found"}
                      for i in body.get("provider_ids", [])]
            return 200, {"ok": True, "claims": claims}
        if (method, path) == ("POST", "/v1/transactions"):
            if "transactions" in cfg["override"]:
                return cfg["override"]["transactions"]
            if not cfg["signed_in"]:
                return 409, {"ok": False, "error": "Not signed in to the provider.", "needs": "signin"}
            since, until = body.get("since") or "0000-00-00", body.get("to") or "9999-99-99"
            keep = [t for t in cfg["transactions"] if not isinstance(t, dict) or since <= str(t.get("date")) <= until]
            return 200, {"ok": True, "provider": PROVIDER, "transactions": keep, "claims": cfg["claims"]}
        if method == "POST" and path in ("/v1/preview", "/v1/submit"):
            return self.claim(path.rsplit("/", 1)[1], body)
        return 404, {"ok": False, "error": "no such call"}

    def claim(self, kind, body):
        cfg = self.config
        if kind in cfg["override"]:
            return cfg["override"][kind]
        if not cfg["signed_in"]:
            return 409, {"ok": False, "error": "Not signed in to the provider.", "needs": "signin"}
        exp = body.get("expense") or {}
        fields = exp.get("fields") or {}
        missing = [f for f in cfg["needs_fields"] if not fields.get(f["key"])]
        if missing:
            return 422, {"ok": False, "error": "The form needs more details.", "needs_fields": missing}
        amount = round(float(exp.get("amount", 0)), 2)
        gst = round(float(fields["gst"]), 2) if fields.get("gst") else round(amount / 11, 2)
        warnings = list(cfg["warnings"])
        if exp.get("id") in self.submitted:
            warnings.append("Already claimed: reference " + self.submitted[exp["id"]])
        if kind == "preview":
            return 200, {"ok": True, "provider": PROVIDER, "kind": "running cost", "car": exp.get("car", {}).get("name", ""),
                         "amount": amount, "gst": gst, "ex_gst": round(amount - gst, 2), "fields": fields,
                         "summary": [f"Claim ${amount:.2f} for {exp.get('category')}", f"Dated {exp.get('date')}"],
                         "warnings": warnings}
        if body.get("confirm") is not True:
            return 400, {"ok": False, "error": "confirm must be true"}
        if warnings:
            return 400, {"ok": False, "error": warnings[0]}
        n = len(self.submitted) + 1
        self.submitted[exp["id"]] = f"REF{n:04d}"
        return 200, {"ok": True, "provider_id": f"P{n:04d}", "reference": f"REF{n:04d}", "status": "Submitted",
                     "amount": amount}

    def invoice_bytes(self, body):
        """The invoice the last request carried, decoded (None if it carried none)."""
        invoice = (body or {}).get("invoice")
        return base64.b64decode(invoice["data_base64"]) if invoice else None

    # ---- serving it -----------------------------------------------------------------------------

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):   # noqa: N802 - http.server's names
                self.reply(*fake.handle("GET", self.path, self.headers.get("Authorization"), {}))

            def do_POST(self):  # noqa: N802
                body = self.body()
                if self.path == "/_fake/config":
                    for key, value in body.items():
                        fake.config[key] = {k: tuple(v) for k, v in value.items()} if key == "override" else value
                    return self.reply(200, {"ok": True})
                if self.path == "/_fake/reset":
                    fake.reset()
                    return self.reply(200, {"ok": True})
                self.reply(*fake.handle("POST", self.path, self.headers.get("Authorization"), body))

            def log_message(self, *args):
                pass

        return Handler

    def start(self, host="127.0.0.1", port=0):
        self.server = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://{host}:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


if __name__ == "__main__":
    fake = Fake()
    host = "0.0.0.0"  # nosec B104 - the dev stack's own network only, never published
    server = ThreadingHTTPServer((host, int(sys.argv[1]) if len(sys.argv) > 1 else 8765), fake.handler())
    server.serve_forever()
