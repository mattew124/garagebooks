# Notifications

GarageBooks can push a notice to your phone when something needs you. You give it an address
(ntfy, an Apprise API, or a Home Assistant webhook) on the **Settings** page, and a
[scheduled job](reference.md) sends the notices. With no address set, nothing is sent.

## What you are told, and when

| Notice | When | Sent again? |
|---|---|---|
| **A reminder is due soon** | The first time a [reminder](reminders.md) comes inside its lead time (by date or by odometer). | No: once. |
| **A reminder is overdue** | The first time it passes its date or odometer reading. | No: once. |
| **Registration expires** and **Lease ends** | The same two reminders that a car's [lease](leases.md) creates. A registration the provider renews for you is not sent. | As for any reminder. |
| **OneDrive client secret expires** | 30 days and again 7 days before the expiry date you entered on the Invoice storage page. | No: once for each. |
| **OneDrive client secret has expired** | Once the date has passed. | No: once. |
| **OneDrive sign-in lost** | When the app finds that Microsoft no longer accepts the saved sign-in. | Once each time it is lost. |

Each notice names the car, the reminder and what it is due on. A reminder with an odometer reading that
isn't known (TeslaMate absent or held back, and no reading entered by hand) is told on its date alone:
the odometer is never guessed.

**Told again when it changes.** Changing a reminder's due date or odometer reading makes it a new event,
so it is told again when it next comes due. Resolving a reminder forgets what was said about it, so one
you reopen is told again. What has been sent is kept for a year, then forgotten.

**A burst is spread out.** At most 10 notices go in one pass (the job runs every 15 minutes). The rest
follow on the next passes. A notice counts as sent only once the service accepted it, so one that failed
is tried again.

**How the sign-in loss is noticed.** Whenever the app uses OneDrive and Microsoft refuses the saved
sign-in, it notes the time. Because you may not use OneDrive for days, the job also asks Microsoft once a
day (a normal token refresh, which also keeps the sign-in from going stale) while the app is signed in.
Connecting again clears it.

!!! note "Not yet"
    Two more notices are planned with the features they report on: **new invoices waiting for review**
    (roadmap item 13, watching the invoice folder) and **a lease budget heading for a shortfall** (L3).
    They are not sent yet. See the [roadmap](roadmap.md).

## Setting it up

Open **Settings**, find **Notifications**, enter the address, choose the format and press **Save
notifications**. Then press **Send a test notice** to check it arrives.

<!-- screenshot: settings-notifications - The Notifications section of the Settings page, with an address saved and the test notice sent -->

!!! warning "The address is a secret"
    For most of these services the address is the only thing protecting your notices: anyone who has an
    ntfy topic name can read it and post to it, and anyone who has a webhook id can trigger it. So
    GarageBooks treats it like a password. The field is a password field, the saved address is never
    shown back (only its host, so you know which one is set), and it is never written to the log, the
    change history or an error message. To change it, enter a new one; to remove it, tick **Remove the
    saved address**. A blank box keeps the one saved.

    Pick a hard-to-guess topic or webhook id, and back the address up with your other secrets: it is
    stored in the `garagebooks` schema, unencrypted, so a database backup holds it.

The address must start with `http://` or `https://`. Because you choose it, and the app is only
reachable by you, an address on your own network (a self-hosted ntfy) works. Redirects are not followed:
use the final address. A login can go in the address (`https://name:password@ntfy.example/topic`) and is
sent as Basic authentication.

### ntfy

Format: **ntfy**. The address is the topic's, on ntfy.sh or your own server.

```text
https://ntfy.example/your-topic
```

The message is sent as plain text, with the title and an emoji tag as ntfy headers. A title with
characters outside plain ASCII (a car called "Zoë's") is encoded as ntfy documents.

### An Apprise API

Format: **JSON**. Use the address of a configuration on your Apprise API server.

```text
https://apprise.example/notify/your-key
```

The notice is posted as JSON with `title` and `body`, which the Apprise API's `/notify` endpoint reads.

### A Home Assistant webhook

Format: **JSON**. Make an automation with a **Webhook** trigger and use its address.

```text
https://homeassistant.example/api/webhook/your-webhook-id
```

The notice is posted as JSON with `title` and `message` (and `body`, the same text). In the automation,
read them as `trigger.json.title` and `trigger.json.message`, for example to pass them to a
`notify` action.

## The test button

**Send a test notice** sends one notice now, to the address saved (save first if you have just changed
it), and says on the page how it went: sent, or in words why not (for example *the service answered HTTP
403*, or *could not be reached*). Neither ever includes the address.

## Seeing what the job did

The notices are sent by the `notifications` scheduled job, which runs every 15 minutes in the app or in
the [worker container](reference.md), wherever `SCHEDULER` puts the jobs. Its last run (when, and
*3 sent*, *nothing new*, *off*, or why it failed) is shown with the other jobs on the Settings page.
With `SCHEDULER=off` no notices are sent.

## Upgrading

The app adds the table and columns it needs itself. If you use the restricted database role
(`scripts/provision-db.sh`), run that script again so the role can use the new `notification` table.
