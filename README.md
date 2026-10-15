# Bandroom

Bandroom is a private, responsive workspace for a music band. It uses Python's standard library for the web server and SQLite for persistent, relational storage. The browser UI is plain HTML, CSS, and JavaScript, so setup does not require a package install or a build step.

## Run a band space

Requirements: Python 3.10 or later (for `zoneinfo`); no third-party Python or JavaScript packages are required.

```sh
cp .env.example .env
set -a
. ./.env
set +a
python3 app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first person to create an account sets the band name and becomes its admin. From there, an admin can create invitation links for members. The default database is `data/band.sqlite3`; it is created on first run and persists between restarts.

The `.env.example` values are local development defaults. The app does not read `.env` by itself; the shell commands above load it before starting the server. Set `BAND_DB` to a protected path for another database. The server binds to `127.0.0.1` by default. For a real deployment, run behind HTTPS, set `COOKIE_SECURE=1`, keep the SQLite file and backups private, and use a production web server/reverse proxy appropriate to your host.

## Isolated fictional demo

The demo uses its own database and fictional members, songs, availability, and rehearsal. It refuses to overwrite an existing database.

```sh
python3 seed_demo.py
BAND_DB=data/demo.sqlite3 python3 app.py
```

All demo accounts use the same demo-only password, `BandroomDemo2026!`:

- `riley@northbound.test` (admin)
- `jordan@northbound.test`
- `sam@northbound.test`
- `casey@northbound.test`

Do not reuse the demo password for a real account.

## Main features

- Individual accounts use salted PBKDF2 password hashes and opaque, server-stored sessions. Invitation tokens are single use and expire after seven days. New members get distinct calendar colors; profiles prevent active members from choosing the same color.
- Band data is scoped on every authenticated API query. Admins can invite, promote, and deactivate members; members can update their own name, calendar color, and instrument profile.
- Songs include status, live-performance history, vocals and piano assignments, key, BPM, duration, difficulty, priority, tags, reference links, arrangement/practice notes, comments, and member votes. Edits use version checks and return a conflict instead of replacing a newer edit.
- Practice includes personal one-off and weekly availability, all-day unavailable entries, recurring-date exceptions, a weekly calendar/agenda, and overlap suggestions based on actual time intervals. No entry remains “not submitted.”
- Rehearsals retain their own time, status, invited members and attendance, saved location details, setlist, goals, notes, summary, cost split, venue payer, and reimbursement status. Availability conflicts are flagged without moving a scheduled rehearsal.
- Saved location rates estimate new rehearsal costs. The estimated/overridden cost is stored on the rehearsal so a later rate edit does not rewrite history. Cost shares use integer cents and avoid division by zero.
- The band timezone defaults to `America/Toronto`. One-off times inside the spring-forward gap are rejected, and a weekly occurrence that falls inside that gap is skipped. Repeated fall-back times use the earlier occurrence by default; the scheduling helpers also accept the later fold for integrations/tests.

## Tests

Run the domain and HTTP API tests with:

```sh
python3 -m unittest discover -v
```

The tests cover partial and multiple-period overlaps, weekly recurrence and exceptions, daylight-saving transitions, rounding and zero-member cost splits, band data isolation, authentication and admin permissions, and creating/editing songs and rehearsals.

## Current scope

This starter is configured for one band per deployment/database. Invitation links are displayed for an admin to share; the app does not send email. Payment fields only record reimbursement state and do not process money. The supplied server is designed for local development; deploy behind HTTPS and a production server before exposing it publicly.
