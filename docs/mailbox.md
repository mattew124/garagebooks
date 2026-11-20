# Invoices by email

Garages, insurers and parts shops often send their invoices by email, and a refund usually arrives as an
email with no attachment at all. GarageBooks can read them from an Outlook.com mailbox: you choose a mail
folder for each car, and its emails are offered on the same [import page](importing.md) as a folder of PDFs.

- Each **PDF attached** to an email is one row, read the way an invoice in OneDrive is read.
- An email with **no PDF** (a refund notice, a receipt in the email itself) is one row, read from its subject
  and text.
- A **refund** is a negative amount, as anywhere else in GarageBooks.

Nothing is saved until you press **Import**, and nothing in the mailbox is ever changed: emails are not
moved, marked as read, flagged or deleted.

## Turning it on

The mailbox signs in with the [Microsoft account](microsoft-account.md) app registration, so set that up first (the
encryption key and the registration; connecting OneDrive itself is optional). Then, on the **Invoice storage** page:

1. Under **Invoices by email**, press **Connect mailbox**, and sign in as the account whose mailbox gets the
   invoices. It can be the same Microsoft account as the OneDrive, or a different one. The app asks only for
   delegated `Mail.Read` (read mail) and `User.Read` (your name, to show who is connected), plus
   `offline_access`.
2. Under **Mailbox folder per car**, press **Choose mailbox folder** beside each car, open the folder its emails
   are filed in, and press **Use**. Only the emails directly in that folder are read, never its subfolders. The top
   of the mailbox can't be chosen.

A folder of its own for each car works best, with an Outlook rule that files the garage's emails into it (or
drag them in by hand). GarageBooks can't create the folder, because it only has permission to read.

<!-- screenshot: storage-mailbox - The Invoices by email card on the Invoice storage page, with a mailbox connected and a folder chosen for a car -->

### The app registration

Nothing needs adding to the registration: the mailbox signs in through the same redirect URI with the same
client ID and secret as OneDrive. If you added the registration's **API
permissions** by hand, add `Mail.Read` and `User.Read` (Microsoft Graph, delegated) as well, or simply consent
to them when you connect. The mailbox has its own sign-in, separate from OneDrive's, so an installation that only
uses OneDrive never grants access to mail, and disconnecting one leaves the other connected. Entering a new client
secret disconnects both.

## Importing

Press **Import emails** beside the car. Each row shows the attachment's name (or, for an email with no PDF, its
subject), with the day it arrived, the sender and the subject below it. An attachment's name opens the PDF in the
browser, and the **email** link beside it opens the email in Outlook on the web; an email with no PDF opens the email. The guesses come from the PDF, or the
email's text, as described in [How it guesses](importing.md#how-it-guesses), and then:

- **Refunds.** A subject that says *refund*, *credit note*, *reversal* or *reimbursement*, or text such as
  *we have refunded*, *your refund* or *refund amount*, makes the amount negative. The figure beside those words
  (*We have refunded $45.00*), or on the line just above *Refunded* (as some insurers lay it out), is used first,
  then the email's total. Small print such as "see our refund policy"
  doesn't count.
- **Date.** A date the text labels as its own (`Invoice date`) comes first; otherwise the day the email arrived,
  in the app's time zone.
- **Vendor.** The company the text names comes first, as for a PDF; otherwise the sender's name.

**Where the PDF goes.** When the car has a folder in its [invoice storage](onedrive.md) (OneDrive or the
[folder on the server](local-folder.md)), an imported attachment is saved there like an uploaded invoice (in the
category's subfolder, named from the [file name template](onedrive.md#file-names)) and the expense links that copy.
Without one, or if saving it fails, the expense links to the email in Outlook on the web instead. An email with no
PDF always links to the email.

**Once only.** An imported email (or each PDF in it) is never offered again. **Skip** works as for files.
Deleting the expense offers the email again. Only the 200 newest emails in the folder are looked at.

<!-- screenshot: import-mail - The import page for a car's mailbox folder: an attached invoice and a refund email with a negative amount -->

## Being told about new emails

The `mail` [scheduled job](scheduled-jobs.md) looks in each car's mailbox folder every 30 minutes and counts the
emails not yet imported or skipped. The count shows beside the folder on the Invoice storage page, and with
[notifications](notifications.md) set up you are told once for each new email that arrives. The job only reads:
importing is always yours to do, on the import page. If Microsoft stops accepting the mailbox's sign-in, the job
notes it and a notice says so; press **Connect mailbox** again.

## Limits

- Outlook.com and other personal Microsoft accounts. A work or school mailbox (Microsoft 365) needs a
  registration that allows organisational accounts, and isn't tested.
- PDF attachments up to 20 MB. Other attachments (images, Word files) are ignored, so an email whose only
  attachment is a photo of a receipt is read from its text.
- Emails are matched by their Outlook link. Moving an imported email out of the folder and back can offer it
  again; the [duplicate check](importing.md#possible-duplicates) then flags it.
- To revoke the app's access to the mailbox entirely, remove it at
  [account.live.com/consent/Manage](https://account.live.com/consent/Manage). That also removes OneDrive's access,
  as it is the same app.
