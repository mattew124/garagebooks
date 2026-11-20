# OneDrive uploads

With this on, the expense form takes the invoice PDF itself. The app uploads it to a subfolder of
the folder you chose for that car, names it from the expense (`261107 Tyres - Tyre Shop.pdf`) and
stores the file's OneDrive link as the expense's invoice link. It works with a personal Microsoft
account (OneDrive Personal). To keep the PDFs on your own server instead, see
[Invoice folder on the server](local-folder.md); with both on, each car uses one or the other.

## Turning it on

1. **Add an encryption key.** Add a key to `.env` and restart the app, which creates its own tables:

    ```bash
    GARAGEBOOKS_ENCRYPTION_KEY=<output of: openssl rand -base64 32 | tr '+/' '-_'>
    ```

    The key encrypts the client secret and the saved sign-in (a refresh token for your whole
    OneDrive) before they go into the database, so a database dump or backup on its own can't
    open the drive. Keep the key out of those backups (see [Backup and restore](backup.md)). If you lose or change it, enter the
    secret and connect again. Up to 0.4.0-rc1 the key was called `EXPENSES_ENCRYPTION_KEY`; that
    name still works.

2. **Register an app with Microsoft** (once per installation; see
    [Microsoft account](microsoft-account.md)), then enter its client ID and secret on
    the app's **Invoice storage** page, in its Microsoft account card, which shows the exact redirect URI to register.

3. **Connect OneDrive.** You sign in on Microsoft's page and come straight back. The app asks
    for delegated `Files.ReadWrite` and `offline_access`.

4. **Choose a folder for each car**, under **Folder per car** on the same page, by browsing your OneDrive, creating one with **New folder**
    if it doesn't exist yet. Uploads go by the folder's ID, so renaming or moving it later
    doesn't break them.

## Where files go

Each invoice goes into a subfolder of the car's folder, set per category on the **Categories** page.
The standard categories start mapped (Tyres, filters, alignment and rotation to `Maintenance`;
Insurance and Registration to folders of the same name); a blank box means a folder named after the
category. A subfolder is made when first needed, and found again whatever its case.

## File names

Files are named `%date %category - %vendor` by default (date, category, vendor). An existing name gets
a suffix; nothing is ever overwritten. The date part is `yymmdd` unless you change it under
**Settings → Invoice file name date** (for example `yyyy-mm-dd`). The whole name is a template under
**Settings → Invoice file name**, a single line of `%` aliases:

| Alias | Becomes |
|---|---|
| `%date` | the invoice date, in the date format above |
| `%category` | the category, e.g. Tyres |
| `%vendor` | the vendor or shop (left out when blank) |
| `%car` | the car's name |
| `%amount` | the amount paid, e.g. 1234.50 |
| `%%` | a percent sign |

For example `%date - %category - %vendor` gives `261107 - Tyres - Tyre Shop.pdf`. It must include
`%date` and `%category` or `%vendor`. A blank alias is left out together with the separator before it
(no vendor gives `261107 - Tyres.pdf`). It applies to new uploads and to renames on import; files
already in OneDrive keep their names.

## Choosing a file already in OneDrive

If the invoice is already in OneDrive, press **Choose from OneDrive** in the invoice box instead.
A window opens over the form, in the car's folder: open a folder to go into it, or **Up** to go above
it, all the way to the top of your drive. Choosing a PDF makes its link the invoice link and fills in
the form from what can be read in it, as an uploaded file does. Nothing is copied or moved.

## Limits

PDFs only (checked by content, not just the name), up to 20 MB. Uploading on the edit page
replaces the link, and the page says so after saving; the old file stays in OneDrive. If OneDrive
isn't connected, or the car has no folder, the expense is still saved and the notice says the PDF wasn't
uploaded. Disconnecting deletes the saved sign-in;
to revoke the app's access entirely, remove it at
[account.live.com/consent/Manage](https://account.live.com/consent/Manage).

## Registering the app

The app registration is shared with the [mailbox](mailbox.md), so it has a page of its own:
[Microsoft account](microsoft-account.md).
