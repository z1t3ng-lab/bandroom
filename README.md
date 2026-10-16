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

## Deploy on Render

Render Free web services have a temporary filesystem, so use a hosted PostgreSQL database for real band data. Create a database, then add its private connection string in Render as `BAND_DATABASE_URL`. Do not commit or share the connection string. Set `COOKIE_SECURE=1` in Render.

Use these Render commands:

- Build: `pip install -r requirements.txt`
- Start: `python3 app.py`

Bandroom creates its tables on first startup. It does not copy existing SQLite data to PostgreSQL. After deployment, create the admin account on the hosted website and share its HTTPS address. Free database plans and limits can change, so keep backups of important band information.

## Tests

```sh
python3 -m unittest discover -v
```
