# Bandroom

Bandroom is a private, responsive workspace for a music band. Local development uses SQLite. Hosting can use PostgreSQL through `BAND_DATABASE_URL`, keeping shared data outside a web host's temporary filesystem.

## Run locally

Requirements: Python 3.10 or later.

```sh
python3 app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first person to create a band workspace becomes that band's admin. The local database is created at `data/band.sqlite3`.

## Multiple bands

One Bandroom deployment can host multiple private band workspaces. Choose **Create a band** on the sign-in screen to set up another workspace; band names must be unique on that site. Songs, members, availability, rehearsals, and locations stay scoped to their band. Members join through that band admin's invitation link.

To sign in, enter the exact band name along with the member email and password. Accounts are band-specific; a person who belongs to two bands will have an account in each band.

## Password reset email

Sign-in includes a **Forgot your password?** link. The member enters their band name and account email. If they match an active account, Bandroom emails a one-time link that expires after 30 minutes. The member chooses a new password on the site. The link is stored as a hash, can only be used once, and resetting the password signs out that member's existing sessions.

Password reset requires an SMTP email provider. Add these environment variables to the server (for example, in Render's service **Environment** settings):

| Variable | Value |
| --- | --- |
| `BANDROOM_PUBLIC_URL` | The public HTTPS address of the app, such as `https://bandroom.onrender.com` |
| `SMTP_HOST` | The SMTP hostname supplied by your email provider |
| `SMTP_PORT` | Usually `587` for STARTTLS, or `465` for TLS |
| `SMTP_USERNAME` | SMTP username from the provider |
| `SMTP_PASSWORD` | SMTP password or provider-generated API credential; keep it secret |
| `SMTP_FROM_EMAIL` | Sender address approved by your provider |
| `SMTP_FROM_NAME` | Optional display name; defaults to `Bandroom` |

Use an email delivery provider that permits transactional password-reset messages, and follow its instructions to verify the sender address. These settings are optional for local use, but password reset will show a setup message until all required values are configured. Never put real SMTP credentials in GitHub, `.env.example`, or a message. After changing Render environment settings, redeploy the service.

## Deploy on Render

Render Free web services have a temporary filesystem, so use a hosted PostgreSQL database for real band data. Create a database, then add its private connection string in Render as `BAND_DATABASE_URL`. Do not commit or share the connection string. Set `COOKIE_SECURE=1` in Render. To enable password resets, also add the SMTP variables above, using your real Render URL for `BANDROOM_PUBLIC_URL`.

Use these Render commands:

- Build: `pip install -r requirements.txt`
- Start: `python3 app.py`

Bandroom creates its tables on first startup. It does not copy existing SQLite data to PostgreSQL. After deployment, create the admin account on the hosted website and share its HTTPS address. Free database plans and limits can change, so keep backups of important band information.

## Tests

```sh
python3 -m unittest discover -v
```
