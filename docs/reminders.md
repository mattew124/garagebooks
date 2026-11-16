# Reminders

Reminders keep track of what a car needs next: a tyre rotation every 10,000 km, a service every six
months, the registration. A reminder falls due on a date, on an odometer reading, or on whichever
comes first. Open **Reminders** in the header to see what is open for all your cars, most urgent first.

<!-- screenshot: reminders-page - The Reminders page: open reminders with their status, the add form and the resolved list -->

Nothing is sent to you. What is due is worked out each time you open a page (or the dashboard), so the
header's **Reminders** link shows how many are overdue or due soon, and the list of expenses carries a
short notice when something needs a look. Notifications are a later part of the
[roadmap](roadmap.md).

## Adding a reminder

Choose a car and say what it is for, then give **a date, an odometer reading, or both**. With both, it
falls due on whichever comes first.

| Field | What it does |
|---|---|
| Category | Optional. When you save an expense in this category, the reminder is offered for resolving, already ticked. |
| On this date / At this odometer reading | When it is due. At least one is needed. |
| Warn me this many days / km before | The lead time: how early it counts as **due soon**. It starts at 30 days and 1,000 km; change it for each reminder. |
| Every (months) / Every (km) | Makes it repeat. Leave both empty for a one-off. |
| Notes | Anything worth remembering, such as the part number. |

An error is shown beside the field it is about, and what you typed is kept.

## What the statuses mean

Each reminder shows a word as well as a colour:

| Status | When |
|---|---|
| **Overdue** | Past its date, or at or over its odometer reading. |
| **Due soon** | Within its lead time of the date or the reading (a reminder due today is due soon until the day is over). |
| **Upcoming** | Neither is close yet. |
| **Odometer unknown** | It is due by odometer, but no odometer is known for the car. It is listed with the due-soon ones and counted in the header: it is never treated as "not due". With a date as well, the date decides, and the page says the odometer is unknown. |
| **Resolved** | Done. Resolved reminders are kept. |

The page shows the car's current odometer and where it came from. That is TeslaMate's, when it tracks
the car and can be read, or else your latest reading from the **Cars** page (an odometer entered on an
expense counts as one). TeslaMate isn't needed: with none, enter readings by hand and odometer reminders
work the same.

## Resolving a reminder

Resolve a reminder by linking the expense that did the job. There are two ways:

- **From the reminder**: choose **Resolve**, then pick one of the car's recent expenses. If there isn't
  one, choose "No expense", and give the date and, if you like, the odometer.
- **From the expense**: after you save an expense for a car that has open reminders, the next page
  offers them, with those in the same category as the expense already ticked. Editing an expense
  has the same link, **Resolve a reminder with this expense**.

A resolved reminder stays in the **Resolved** list, newest first, with the expense that did it. If you
resolved one by mistake, **Reopen** it.

### Repeating reminders

When you resolve a repeating reminder, the next one is created for you:

- its **date** is the day it was done plus the months you chose, in calendar months (31 October plus
  six months is 30 April: it never runs past the end of the month);
- its **odometer reading** is the odometer when it was done plus the distance you chose. That is the
  expense's odometer, else the one you entered, else the best known on that date, else the previous
  reading it was due at;
- the title, category, lead times, repeat and notes carry over.

If no odometer is known to count from, the reminder says so and the next one is not created: add it
yourself. Reopening a reminder removes the next one it created, unless you have edited or resolved that
one since, in which case it is left alone and the page says so.

## Registration and lease end

If a car has a [lease](leases.md), two reminders appear by themselves: **Registration expires**, from the
lease's registration date, and **Lease ends**, with a lead time of 90 days. They are read from the lease
each time, so change the date on the lease and they follow; they disappear when the lease does. They
can't be resolved or deleted here, and link to the lease page. When the provider renews the registration
(the lease's "The provider renews it" box), **Registration expires** is for your information only and
is not counted in the header.

## On the dashboard

The Grafana dashboard has a **Reminders** table for the selected car: the title, due date and
reading, days and kilometres left, and the status. See [What the dashboard counts](dashboard.md#reminders).

<!-- screenshot: reminders-dashboard - The Reminders table on the dashboard -->

## Upgrading

The app adds the reminders table and view itself when it starts. If you use the restricted database
role (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` again first.
