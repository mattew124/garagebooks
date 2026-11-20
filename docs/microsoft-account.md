# Microsoft account

[OneDrive uploads](onedrive.md) and [invoices by email](mailbox.md) both sign in to Microsoft through one app
registration of your own, entered once in the **Microsoft account** card of the **Invoice storage** page. Each is then
connected on its own, in its own card, with a sign-in that asks only for what it needs: OneDrive for its files, the
mailbox to read mail. They can be different Microsoft accounts, and disconnecting one leaves the other connected.
Entering a new client secret disconnects both.

It needs `GARAGEBOOKS_ENCRYPTION_KEY`, which encrypts the client secret and both sign-ins in the database (see
[Turning it on](onedrive.md#turning-it-on)).

## Registering the app

Any Microsoft account can register the app, including the personal one that owns the OneDrive.
It needs no paid Azure subscription: Microsoft creates a free directory for personal-account
registrations. The account choice that matters comes later, at **Connect OneDrive**, where you
sign in as the account whose OneDrive gets the invoices, and at
**Connect mailbox**, the account whose mailbox they arrive in.

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), go to **Identity** →
    **Applications** → **App registrations** → **New registration**.
2. **Name:** anything, e.g. `garagebooks`.
3. **Supported account types:** **Personal Microsoft accounts only**. "Accounts in any
    organizational directory and personal Microsoft accounts" also works.
4. **Redirect URI:** platform **Web**, and the address shown in the Microsoft account card of the app's Invoice
    storage page, `https://<your host><BASE_PATH>/microsoft/callback`. It must match byte for byte, scheme
    and path included, and Microsoft accepts plain `http://` only for `localhost`. Then
    **Register**.
5. **Authentication:** leave **Allow public client flows** at **No** (the default). Turning it on
    makes Microsoft treat the app as a public client, and every token refresh then fails with
    `AADSTS90023: Public clients can't send a client secret`.
6. **API permissions** (optional): **Add a permission** → **Microsoft Graph** →
    **Delegated permissions** → `Files.ReadWrite`, `Mail.Read`, `User.Read` and `offline_access`. If you
    skip this, you're asked to consent at the first sign-in: OneDrive asks for `Files.ReadWrite`, the
    mailbox for `Mail.Read` and `User.Read`.
7. **Certificates & secrets** → **Client secrets** → **New client secret**. Copy its **Value**
    straight away; it's shown only once, and the **Secret ID** next to it is not the secret.
    Secrets last at most 24 months, so note the **Expires** date. With [notifications](notifications.md)
    set up, you are told 30 days and 7 days before it, and when it has passed.
8. From **Overview**, copy the **Application (client) ID**. It isn't secret.
9. On the app's **Invoice storage** page, paste the client ID, the secret's value and its expiry date,
    then press **Connect OneDrive** or **Connect mailbox**.

[Notifications](notifications.md) can also tell you once if a saved sign-in stops working, so you find
out before an invoice fails to upload.

A dev stack or second installation needs its own callback address: add it as another **Web**
redirect URI on the same registration.

The same registration also reads invoices from an Outlook.com mailbox, with a sign-in of its own that only
reads mail: see [Invoices by email](mailbox.md). Nothing more needs registering for it.

A redirect URI you have just added can take a few minutes to take effect. If connecting
shows Microsoft's "invalid request" error straight after adding one, and the address matches
exactly, wait a few minutes and press it again.

Why the normal web sign-in and not a device code or the "nativeclient" redirect: both have
been tried with personal accounts and fail. A device-code sign-in marks the app as a public
client and breaks refreshes (`AADSTS90023` above). Microsoft's out-of-band
`https://login.microsoftonline.com/common/oauth2/nativeclient` page now redirects to an error
page after a few seconds, which made rclone and the
[abraunegg/onedrive](https://github.com/abraunegg/onedrive/discussions/3558) client drop it too.

References: [Register an application](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app) ·
[Authorization code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) ·
[Graph permissions](https://learn.microsoft.com/en-us/graph/permissions-reference) ·
[Remove an app's access to your account](https://account.live.com/consent/Manage)

Every installation registers its own app because Microsoft only redirects to addresses
registered on the app, and each installation has its own. Sign-in uses PKCE, and the
callback must return to the browser that started it.

The redirect URI is built from the proxy's `X-Forwarded-Proto` and `X-Forwarded-Host`
headers, so the proxy must set them (Traefik does).


## An older registration

Before the mailbox, the redirect URI was `…/onedrive/callback`. A registration saved then keeps using that address,
so nothing stops working; the Microsoft account card says so and shows the new one. To move, add
`…/microsoft/callback` as another **Web** redirect URI on the registration, save the registration on the Invoice
storage page again (the client secret too), and connect OneDrive and the mailbox again. The old address can then be
removed from the registration.
