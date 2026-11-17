# Claims connector

If you pay running costs through a novated lease, you claim them back from your salary packaging
provider. GarageBooks can send an expense and its invoice to a **claims connector**, show you what will
be claimed, submit it when you confirm, and remember the claim so nothing is claimed twice. The
connector is a separate service that knows how to talk to one provider (usually by driving that
provider's website). GarageBooks doesn't include one, because each provider is different and its
sign-in is yours alone. This page describes how to turn the feature on, and the protocol a connector
speaks, so you can write one for your provider.

## Turning it on

Set the connector's address in `.env`, then restart the app. With `CLAIMS_CONNECTOR_URL` unset (the
default) nothing about claims appears: no buttons, no pages.

```
CLAIMS_CONNECTOR_URL=http://claims-connector:8765
CLAIMS_CONNECTOR_TOKEN=a-long-random-string
```

| Setting | Meaning |
| --- | --- |
| `CLAIMS_CONNECTOR_URL` | The connector's address, without `/v1`. `http://` or `https://`. |
| `CLAIMS_CONNECTOR_TOKEN` | The bearer token the connector expects. |
| `CLAIMS_CONNECTOR_TOKEN_FILE` | A file holding the token (a Docker or Kubernetes secret, say), read at each request, so it can be rotated without a restart. Used instead of `CLAIMS_CONNECTOR_TOKEN` when both are set. |

Keep the connector on a private network the app can reach, never on the internet. It holds a sign-in to
your provider.

A call can take a minute, because a connector may be driving a browser. The app waits up to 150 seconds
for an answer, and the container's web server allows a request 180 seconds. If you run the app some
other way than the image, give your web server at least that.

## What is offered

An expense can be claimed when all of these hold, and the **Claim** link is shown only then:

- its category belongs to one of the provider's budget pools other than **lease payments**: fuel or
  charging, tyres, maintenance, insurance, registration or other (set a category's pool on the
  Categories page);
- the amount is more than nothing (a refund can't be claimed);
- the car has a [lease](leases.md) covering the expense's date;
- no claim has been recorded for it yet.

See [Claiming an expense](leases.md#claiming-an-expense) for what you do in the app. The rest of this
page is for people writing or running a connector.

## The protocol (version 1)

Every request carries `Authorization: Bearer <token>` and, when it has a body, `Content-Type:
application/json`. Every answer is a JSON object with `"ok": true` or `"ok": false`. The path starts
with `/v1`. A connector answers 401 for a wrong token.

### Objects

An **expense**:

```json
{
  "id": 41,
  "car": {"id": 1, "name": "Family car", "make": "Example", "model": "Model A"},
  "pool": "tyres",
  "category": "Tyres",
  "date": "2026-11-07",
  "amount": 1200.00,
  "vendor": "Example Tyres Pty Ltd",
  "notes": "Four tyres",
  "fields": {"gst": "109.09", "description": "New tyres"}
}
```

- `pool` is the category's budget pool: `fuel_charging`, `tyres`, `maintenance`, `insurance`,
  `registration` or `other`.
- `date` is the day the cost was incurred, in the app's time zone (`APP_TZ`).
- `amount` includes GST.
- `fields` holds only what the owner typed, or what the connector asked for in a 422: `gst`,
  `description`, `provider`, `policy_number`, `start`, `end` or `invoice_date`. Dates are `YYYY-MM-DD`
  and money is a decimal string. It is empty when nothing was typed.

An **invoice** is `null`, or:

```json
{"name": "INV12345.pdf", "content_type": "application/pdf", "data_base64": "JVBERi0xLjcK..."}
```

The app sends the invoice file kept in the invoice folder, up to 10 MB. It doesn't send an invoice that
is only a link.

### `GET /v1/health`

Answers 200 with whether the connector is up and signed in to the provider:

```json
{"ok": true, "provider": "Example Salary Packaging", "session_saved": true,
 "signin": {"state": "idle"}}
```

`signin.state` is one of `idle`, `starting`, `code_needed`, `checking`, `signed_in` or `failed`, with an
optional `message` (what to enter) and `error` (why it failed).

### `POST /v1/preview`

Body: `{"expense": EXPENSE, "invoice": INVOICE}`. It works out what would be claimed and sends nothing
to the provider that can't be undone.

200:

```json
{"ok": true, "provider": "Example Salary Packaging", "kind": "Running cost", "car": "Family car",
 "amount": 1200.00, "gst": 109.09, "ex_gst": 1090.91, "fields": {"description": "New tyres"},
 "summary": ["Claim $1,200.00 for Tyres", "Dated 7 November 2026"],
 "warnings": []}
```

`warnings` is a list of sentences. When it isn't empty, for example `"Already claimed: reference
REF0001"`, the app shows it prominently and offers no Submit button, and the connector must refuse a
submit of the same thing.

Other answers:

| Status | Body | The app |
| --- | --- | --- |
| 422 | `{"ok": false, "error": "...", "needs_fields": [{"key": "description", "label": "What was it for?", "type": "text"}]}` | Shows an input for each (`key` is one of `policy_number`, `start`, `end`, `description`, `provider`, `invoice_date`; `type` is `date` or `text`; `label` is the provider's own wording), then previews again with the values in `expense.fields`. |
| 409 | `{"ok": false, "error": "...", "needs": "signin"}` | Offers the sign-in below. |
| 400, 500, 502 | `{"ok": false, "error": "..."}` | Shows the error text. |

### `POST /v1/submit`

Body: `{"expense": EXPENSE, "invoice": INVOICE, "confirm": true}`. The app only sends it when the owner
presses **Submit claim**. A connector must refuse without `"confirm": true`, and must refuse a claim
that has a preview warning.

200:

```json
{"ok": true, "provider_id": "98765", "reference": "REF0001", "status": "Submitted", "amount": 1200.00}
```

`provider_id` identifies the claim in later status checks, and is required: without it the app can't
record the claim. Errors are as for preview.

### `POST /v1/status`

Body: `{"provider_ids": ["98765", "98766"]}`. 200:

```json
{"ok": true, "claims": [
  {"provider_id": "98765", "reference": "REF0001", "status": "Paid", "approved_amount": 1100.00, "paid_date": "2026-11-14"},
  {"provider_id": "98766", "status": null, "error": "not found"}
]}
```

`paid_date` is `YYYY-MM-DD` or `null`. A claim with `"status": null` keeps whatever the app already
knew.

### Signing in

A provider that asks for a code (a text message, say) needs a person. The app drives it like this:

1. A call answers 409 with `"needs": "signin"`. The page offers **Sign in**.
2. `POST /v1/signin` with `{}` answers `{"ok": true, "state": "starting"}`. The app then polls
   `GET /v1/health` every 2 seconds, for up to four minutes.
3. When `signin.state` is `code_needed`, the page shows `signin.message` and a box for the code.
   `POST /v1/signin/code` with `{"code": "123456"}` answers `{"ok": true, "state": "checking"}`, and the
   app keeps polling.
4. `signed_in` carries on to the claim. `failed` shows `signin.error`.

### An example exchange

The owner opens a tyres expense of $1,200.00 dated 7 November 2026. The app calls `POST /v1/preview`;
the connector finds the form needs a description:

```
POST /v1/preview            -> 422 {"ok": false, "error": "The form needs more details.",
                                    "needs_fields": [{"key": "description", "label": "What was it for?", "type": "text"}]}
```

The owner types "New tyres" and presses **Check again**; the app repeats the call with
`"fields": {"description": "New tyres"}` and gets the 200 preview above. The owner reads it, presses
**Submit claim**, and the app calls `POST /v1/submit` with `"confirm": true`. The connector answers
with `provider_id` `98765` and reference `REF0001`, and the app records them against the expense and
shows `REF0001` beside it. A week later, **Refresh claim statuses** calls `POST /v1/status` with
`{"provider_ids": ["98765"]}` and learns the claim is paid.

## What the app stores

One row per claimed expense, in `garagebooks.expense_claim`: the provider's name, `provider_id`,
reference, status, the amount, the fields typed, when it was submitted, and later the approved amount,
paid date and when the status was last checked. The row appears in the [change history](reference.md#change-history).
A claimed expense can't be deleted until you remove its claim record (on its Edit page), which asks
first. Removing the record does not cancel the claim at the provider.

The connector sees the expense and invoice you send it and nothing else. GarageBooks never gives it
database access, and never sends it the other expenses.
