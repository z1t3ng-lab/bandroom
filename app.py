#!/usr/bin/env python3
"""Bandroom: a private band management web app with SQLite or hosted Postgres."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import sqlite3
import time as time_module
import colorsys
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domain import DomainError, availability_suggestions, estimated_cost_cents, expand_availability, local_datetime, split_cost, utc_interval


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BAND_DB", ROOT / "data" / "band.sqlite3")).expanduser()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
BAND_DATABASE_URL = os.environ.get("BAND_DATABASE_URL", "").strip()
SESSION_DAYS = 14
STATUSES = {"suggested", "planned", "learning", "practicing", "ready", "archived"}
ATTENDANCE = {"attending", "maybe", "unavailable", "no_response"}
ROLES = {"drummer", "guitarist", "bassist", "other"}
MEMBER_COLORS = ["#5c9d8b", "#c77c5b", "#8674b6", "#6f91bd", "#bd9142", "#b86681", "#6e9853", "#5487a0", "#a16f4e", "#789a69", "#926f9b", "#b27578"]


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS bands (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'America/Toronto', created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bands_name_unique ON bands(LOWER(name));
CREATE TABLE IF NOT EXISTS members (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 display_name TEXT NOT NULL, email TEXT NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')), roles_json TEXT NOT NULL DEFAULT '[]',
 color TEXT NOT NULL DEFAULT '#5c9d8b', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 UNIQUE(band_id,email)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
 id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_password_resets_member_created ON password_resets(member_id,created_at);
CREATE TABLE IF NOT EXISTS invitations (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, created_by INTEGER NOT NULL REFERENCES members(id),
 expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS songs (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 title TEXT NOT NULL, artist TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'suggested',
 performed_live INTEGER NOT NULL DEFAULT 0, performed_dates_json TEXT NOT NULL DEFAULT '[]',
 singer_ids_json TEXT NOT NULL DEFAULT '[]', pianist_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
 key_name TEXT NOT NULL DEFAULT '', bpm INTEGER, duration_minutes INTEGER, difficulty INTEGER,
 priority INTEGER NOT NULL DEFAULT 3, tags_json TEXT NOT NULL DEFAULT '[]', links_json TEXT NOT NULL DEFAULT '[]',
 arrangement_notes TEXT NOT NULL DEFAULT '', practice_notes TEXT NOT NULL DEFAULT '',
 created_by INTEGER NOT NULL REFERENCES members(id), updated_by INTEGER NOT NULL REFERENCES members(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS song_comments (
 id INTEGER PRIMARY KEY, song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS song_votes (
 song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 value INTEGER NOT NULL DEFAULT 1 CHECK(value IN (1)), created_at TEXT NOT NULL,
 PRIMARY KEY(song_id,member_id)
);
CREATE TABLE IF NOT EXISTS activity (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, entity_title TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_band_created ON announcements(band_id,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS availability (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 kind TEXT NOT NULL DEFAULT 'available' CHECK(kind IN ('available','unavailable')),
 start_date TEXT NOT NULL, start_time TEXT, end_time TEXT,
 recurrence TEXT NOT NULL DEFAULT 'none' CHECK(recurrence IN ('none','weekly')),
 until_date TEXT, start_fold INTEGER NOT NULL DEFAULT 0, end_fold INTEGER NOT NULL DEFAULT 0,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS availability_exceptions (
 availability_id INTEGER NOT NULL REFERENCES availability(id) ON DELETE CASCADE,
 exception_date TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(availability_id,exception_date)
);
CREATE TABLE IF NOT EXISTS locations (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 name TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', map_url TEXT NOT NULL DEFAULT '',
 hourly_rate REAL, flat_cost REAL, contact TEXT NOT NULL DEFAULT '', booking_link TEXT NOT NULL DEFAULT '',
 equipment_json TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
 created_by INTEGER NOT NULL REFERENCES members(id), updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rehearsals (
 id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
 title TEXT NOT NULL, event_type TEXT NOT NULL DEFAULT 'rehearsal' CHECK(event_type IN ('rehearsal','performance')),
 start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, timezone TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','completed','cancelled')),
 organizer_id INTEGER NOT NULL REFERENCES members(id), invited_ids_json TEXT NOT NULL DEFAULT '[]',
 location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
 location_name_snapshot TEXT NOT NULL DEFAULT '', address_snapshot TEXT NOT NULL DEFAULT '',
 room_number TEXT NOT NULL DEFAULT '', booking_link TEXT NOT NULL DEFAULT '', booking_reference TEXT NOT NULL DEFAULT '',
 total_cost_cad REAL NOT NULL DEFAULT 0, split_mode TEXT NOT NULL DEFAULT 'attending', split_member_ids_json TEXT NOT NULL DEFAULT '[]',
 cost_override INTEGER NOT NULL DEFAULT 0, hourly_rate_snapshot REAL, flat_cost_snapshot REAL,
 venue_paid_by_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
 updated_by INTEGER REFERENCES members(id) ON DELETE SET NULL,
 goals TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rehearsal_attendance (
 rehearsal_id INTEGER NOT NULL REFERENCES rehearsals(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 response TEXT NOT NULL DEFAULT 'no_response' CHECK(response IN ('attending','maybe','unavailable','no_response')),
 PRIMARY KEY(rehearsal_id,member_id)
);
CREATE TABLE IF NOT EXISTS rehearsal_songs (
 rehearsal_id INTEGER NOT NULL REFERENCES rehearsals(id) ON DELETE CASCADE,
 song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(rehearsal_id,song_id)
);
CREATE TABLE IF NOT EXISTS cost_shares (
 rehearsal_id INTEGER NOT NULL REFERENCES rehearsals(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
 amount_cents INTEGER NOT NULL DEFAULT 0, reimbursement_paid INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(rehearsal_id,member_id)
);
CREATE INDEX IF NOT EXISTS idx_songs_band ON songs(band_id);
CREATE INDEX IF NOT EXISTS idx_activity_band ON activity(band_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rehearsals_start ON rehearsals(band_id,start_utc);
CREATE INDEX IF NOT EXISTS idx_availability_band_dates ON availability(band_id,start_date,until_date);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


class DatabaseRow:
    """A small row object that supports both row[0] and row['column_name']."""

    def __init__(self, names, values):
        self._names = list(names)
        self._values = tuple(values)
        self._indices = {name: index for index, name in enumerate(self._names)}

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._indices[key]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)


class PostgresCursor:
    def __init__(self, cursor, inserted_id=None):
        self._cursor = cursor
        self._inserted_id = inserted_id
        self._names = [column.name for column in (cursor.description or ())]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._inserted_id

    def _row(self, values):
        return DatabaseRow(self._names, values) if values is not None else None

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(values) for values in self._cursor.fetchall()]

    def __iter__(self):
        for values in self._cursor:
            yield self._row(values)


class PostgresConnection:
    """Adapt psycopg to the small subset of sqlite3 used by this app."""

    ID_TABLES = {"bands", "members", "invitations", "password_resets", "songs", "song_comments", "activity", "announcements",
                 "availability", "locations", "rehearsals"}

    def __init__(self, connection):
        self._connection = connection

    @staticmethod
    def _prepare(sql):
        sql = sql.strip()
        if sql.upper() == "BEGIN IMMEDIATE":
            return "BEGIN", False
        ignored = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", sql))
        if ignored:
            sql = re.sub(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", sql, count=1)
            sql += " ON CONFLICT DO NOTHING"
        sql = sql.replace("?", "%s")
        match = re.match(r"(?is)^INSERT\s+INTO\s+([a-z_]+)", sql)
        returns_id = bool(match and match.group(1).lower() in PostgresConnection.ID_TABLES
                          and "RETURNING" not in sql.upper())
        if returns_id:
            sql += " RETURNING id"
        return sql, returns_id

    def execute(self, sql, parameters=()):
        prepared, returns_id = self._prepare(sql)
        cursor = self._connection.cursor()
        cursor.execute(prepared, tuple(parameters or ()))
        inserted_id = None
        if returns_id:
            row = cursor.fetchone()
            inserted_id = row[0] if row else None
        return PostgresCursor(cursor, inserted_id)

    def executescript(self, script):
        # The Bandroom schema contains no semicolons inside SQL string literals.
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def db_connect() -> sqlite3.Connection | PostgresConnection:
    if BAND_DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import tuple_row
        except ImportError as error:
            raise RuntimeError("BAND_DATABASE_URL is set, but psycopg is not installed") from error
        connection = psycopg.connect(BAND_DATABASE_URL, sslmode="require", row_factory=tuple_row)
        return PostgresConnection(connection)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        if BAND_DATABASE_URL:
            postgres_schema = SCHEMA.replace("PRAGMA foreign_keys = ON;", "").replace(" COLLATE NOCASE", "")
            postgres_schema = re.sub(r"\bINTEGER PRIMARY KEY\b", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", postgres_schema)
            conn.executescript(postgres_schema)
            rehearsal_columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='rehearsals'")}
        else:
            conn.executescript(SCHEMA)
            rehearsal_columns={row[1] for row in conn.execute("PRAGMA table_info(rehearsals)")}
        if "event_type" not in rehearsal_columns:
            conn.execute("ALTER TABLE rehearsals ADD COLUMN event_type TEXT NOT NULL DEFAULT 'rehearsal'")
        if "updated_by" not in rehearsal_columns:
            conn.execute("ALTER TABLE rehearsals ADD COLUMN updated_by INTEGER REFERENCES members(id) ON DELETE SET NULL")
            conn.execute("UPDATE rehearsals SET updated_by=organizer_id WHERE updated_by IS NULL")
        if "cost_override" not in rehearsal_columns:
            conn.execute("ALTER TABLE rehearsals ADD COLUMN cost_override INTEGER NOT NULL DEFAULT 1")
        if "hourly_rate_snapshot" not in rehearsal_columns:
            conn.execute("ALTER TABLE rehearsals ADD COLUMN hourly_rate_snapshot REAL")
            conn.execute("UPDATE rehearsals SET hourly_rate_snapshot=(SELECT hourly_rate FROM locations WHERE locations.id=rehearsals.location_id)")
        if "flat_cost_snapshot" not in rehearsal_columns:
            conn.execute("ALTER TABLE rehearsals ADD COLUMN flat_cost_snapshot REAL")
            conn.execute("UPDATE rehearsals SET flat_cost_snapshot=(SELECT flat_cost FROM locations WHERE locations.id=rehearsals.location_id)")
        if BAND_DATABASE_URL:
            location_columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='locations'")}
        else:
            location_columns={row[1] for row in conn.execute("PRAGMA table_info(locations)")}
        if "version" not in location_columns:
            conn.execute("ALTER TABLE locations ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if not BAND_DATABASE_URL:
            conn.execute("PRAGMA journal_mode = WAL")


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    import base64
    return f"{iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def password_ok(password: str, encoded: str) -> bool:
    try:
        import base64
        iterations, salt, expected = encoded.split("$", 2)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(base64.b64decode(expected), actual)
    except (ValueError, TypeError):
        return False


def valid_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 10 or len(password) > 200:
        raise ApiError(400, "Password must be between 10 and 200 characters")


def password_reset_email_config() -> dict:
    """Read and validate the mail settings needed for account recovery."""
    settings = {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": os.environ.get("SMTP_FROM_EMAIL", "").strip(),
        "from_name": os.environ.get("SMTP_FROM_NAME", "Bandroom").strip() or "Bandroom",
        "public_url": os.environ.get("BANDROOM_PUBLIC_URL", "").strip().rstrip("/"),
    }
    try:
        settings["port"] = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError as error:
        raise ApiError(503, "Password reset email is not configured yet. Please contact the site admin.") from error
    if (not all(settings[key] for key in ("host", "username", "password", "from_email", "public_url"))
            or not 1 <= settings["port"] <= 65535
            or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", settings["from_email"])):
        raise ApiError(503, "Password reset email is not configured yet. Please contact the site admin.")
    public = urlparse(settings["public_url"])
    local_http = public.scheme == "http" and public.hostname in {"localhost", "127.0.0.1", "::1"}
    if not public.hostname or (public.scheme != "https" and not local_http) or public.username or public.password:
        raise ApiError(503, "Password reset email is not configured yet. Please contact the site admin.")
    return settings


def send_password_reset_email(recipient: str, band_name: str, reset_url: str, settings: dict) -> None:
    message = EmailMessage()
    message["Subject"] = "Reset your Bandroom password"
    message["From"] = formataddr((settings["from_name"], settings["from_email"]))
    message["To"] = recipient
    message.set_content(
        f"A password reset was requested for your {band_name} Bandroom account.\n\n"
        f"Choose a new password using this one-time link (it expires in 30 minutes):\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email. Your password will not change."
    )
    timeout = 15
    if settings["port"] == 465:
        with smtplib.SMTP_SSL(settings["host"], settings["port"],
                              context=ssl.create_default_context(), timeout=timeout) as server:
            server.login(settings["username"], settings["password"])
            server.send_message(message)
    else:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=timeout) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(settings["username"], settings["password"])
            server.send_message(message)


def parse_json_list(value: str | None) -> list:
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


class ApiError(Exception):
    def __init__(self, status: int, message: str, details=None):
        self.status, self.message, self.details = status, message, details


def is_database_integrity_error(error: Exception) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return True
    if not BAND_DATABASE_URL:
        return False
    try:
        import psycopg
        return isinstance(error, psycopg.IntegrityError)
    except ImportError:
        return False


def clean_text(value, name: str, max_length: int = 500, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"{name} is required")
    if len(value) > max_length:
        raise ApiError(400, f"{name} must be {max_length} characters or fewer")
    return value


def safe_url(value, name: str) -> str:
    value=clean_text(value,name,500)
    if value and not re.match(r"^https?://",value,re.I):
        raise ApiError(400,f"{name} must start with http:// or https://")
    return value


def member_public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "display_name": row["display_name"], "email": row["email"],
            "role": row["role"], "roles": parse_json_list(row["roles_json"]), "color": row["color"],
            "active": bool(row["active"])}


def pick_member_color(conn: sqlite3.Connection, band_id: int) -> str:
    used={r[0].lower() for r in conn.execute("SELECT color FROM members WHERE band_id=? AND active=1",(band_id,))}
    for color in MEMBER_COLORS:
        if color.lower() not in used:return color
    # Keep distinct colors when a band grows beyond the starter palette.
    for index in range(1000):
        hue=((index * 137.508 + 23) % 360) / 360
        red,green,blue=colorsys.hls_to_rgb(hue,0.72,0.38)
        color="#{:02x}{:02x}{:02x}".format(round(red*255),round(green*255),round(blue*255))
        if color.lower() not in used:return color
    return "#5c9d8b"


def require_member(handler) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row]:
    token = handler.get_cookie("band_session")
    if not token:
        raise ApiError(401, "Please sign in")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = db_connect()
    row = conn.execute("""SELECT m.*,b.name AS band_name,b.timezone AS band_timezone
        FROM sessions s JOIN members m ON m.id=s.member_id JOIN bands b ON b.id=m.band_id
        WHERE s.token_hash=? AND s.expires_at>? AND m.active=1""", (token_hash, now_iso())).fetchone()
    if not row:
        conn.close()
        raise ApiError(401, "Your session has expired. Please sign in again.")
    band = conn.execute("SELECT * FROM bands WHERE id=?", (row["band_id"],)).fetchone()
    return conn, row, band


def require_admin(member: sqlite3.Row) -> None:
    if member["role"] != "admin":
        raise ApiError(403, "Only a band admin can do that")


def record_activity(conn, band_id: int, member_id: int, action: str, entity_type: str, entity_id: int, title: str):
    conn.execute("INSERT INTO activity(band_id,member_id,action,entity_type,entity_id,entity_title,created_at) VALUES(?,?,?,?,?,?,?)",
                 (band_id, member_id, action, entity_type, entity_id, title, now_iso()))


def validate_member_ids(conn, band_id: int, values, label: str = "Members") -> list[int]:
    if not isinstance(values, list):
        raise ApiError(400, f"{label} must be a list")
    try:
        ids = list(dict.fromkeys(int(v) for v in values))
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} contains an invalid member")
    if ids:
        placeholders = ",".join("?" for _ in ids)
        found = {r[0] for r in conn.execute(f"SELECT id FROM members WHERE band_id=? AND active=1 AND id IN ({placeholders})", (band_id, *ids))}
        if found != set(ids):
            raise ApiError(400, f"{label} must be active band members")
    return ids


def validate_json_list(value, name: str, max_items: int = 40) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ApiError(400, f"{name} must be a list of at most {max_items} items")
    return value


def normalize_song(conn, band_id: int, body: dict, previous: dict | None = None) -> dict:
    current = previous or {}
    data = {**current, **body}
    title = clean_text(data.get("title"), "Song title", 160, True)
    artist = clean_text(data.get("artist"), "Original artist", 160, True)
    status = data.get("status", "suggested")
    if status not in STATUSES:
        raise ApiError(400, "Choose a valid song status")
    singers = validate_member_ids(conn, band_id, data.get("singer_ids", []), "Singers")
    pianist = data.get("pianist_id")
    if pianist in (None, ""):
        pianist = None
    else:
        try:
            pianist = int(pianist)
        except (ValueError, TypeError):
            raise ApiError(400, "Pianist must be a band member")
        validate_member_ids(conn, band_id, [pianist], "Pianist")
    def bounded_int(key, low, high, label, optional=False):
        value = data.get(key)
        if value in (None, "") and optional:
            return None
        try:
            number = int(value)
        except (ValueError, TypeError):
            raise ApiError(400, f"{label} must be a number")
        if not low <= number <= high:
            raise ApiError(400, f"{label} must be between {low} and {high}")
        return number
    tags = [clean_text(tag, "Tag", 32) for tag in validate_json_list(data.get("tags", []), "Tags", 30)]
    tags = list(dict.fromkeys(tag for tag in tags if tag))
    links = []
    for item in validate_json_list(data.get("links", []), "Reference links", 20):
        if not isinstance(item, dict):
            raise ApiError(400, "Each reference link needs a label and URL")
        label = clean_text(item.get("label"), "Link label", 80, True)
        url = clean_text(item.get("url"), "Link URL", 500, True)
        if not re.match(r"^https?://", url, re.I):
            raise ApiError(400, "Reference links must start with http:// or https://")
        links.append({"label": label, "url": url})
    dates = []
    for value in validate_json_list(data.get("performed_dates", []), "Performance dates", 100):
        try:
            dates.append(date.fromisoformat(value).isoformat())
        except (ValueError, TypeError):
            raise ApiError(400, "Performance dates must be valid dates")
    return {
        "title": title, "artist": artist, "status": status,
        "performed_live": bool(data.get("performed_live", bool(dates))), "performed_dates_json": json.dumps(dates),
        "singer_ids_json": json.dumps(singers), "pianist_id": pianist,
        "key_name": clean_text(data.get("key_name"), "Key", 20),
        "bpm": bounded_int("bpm", 20, 400, "Tempo", True),
        "duration_minutes": bounded_int("duration_minutes", 1, 300, "Duration", True),
        "difficulty": bounded_int("difficulty", 1, 5, "Difficulty", True),
        "priority": bounded_int("priority", 1, 5, "Priority") if data.get("priority") not in (None, "") else 3,
        "tags_json": json.dumps(tags), "links_json": json.dumps(links),
        "arrangement_notes": clean_text(data.get("arrangement_notes"), "Arrangement notes", 5000),
        "practice_notes": clean_text(data.get("practice_notes"), "Practice notes", 5000),
    }


def song_object(conn, row: sqlite3.Row, detail: bool = False) -> dict:
    result = dict(row)
    result["performed_live"] = bool(result["performed_live"])
    result["performed_dates"] = parse_json_list(result.pop("performed_dates_json", "[]"))
    result["singer_ids"] = parse_json_list(result.pop("singer_ids_json", "[]"))
    result["tags"] = parse_json_list(result.pop("tags_json", "[]"))
    result["links"] = parse_json_list(result.pop("links_json", "[]"))
    result["votes"] = conn.execute("SELECT COUNT(*) FROM song_votes WHERE song_id=?", (row["id"],)).fetchone()[0]
    result["voted_by_me"] = bool(result.pop("member_voted", 0))
    result["singers"] = [dict(r) for r in conn.execute("SELECT id,display_name,color FROM members WHERE id IN (" + (",".join("?" for _ in result["singer_ids"]) or "NULL") + ")", result["singer_ids"])]
    if result.get("pianist_id"):
        p = conn.execute("SELECT id,display_name,color FROM members WHERE id=?", (result["pianist_id"],)).fetchone()
        result["pianist"] = dict(p) if p else None
    else:
        result["pianist"] = None
    for field, output_field in (("created_by", "created_by_member"), ("updated_by", "updated_by_member")):
        user = conn.execute("SELECT id,display_name FROM members WHERE id=?", (result[field],)).fetchone()
        result[output_field] = dict(user) if user else None
    if detail:
        result["comments"] = [dict(r) for r in conn.execute("""SELECT c.id,c.body,c.created_at,m.id AS member_id,m.display_name,m.color
             FROM song_comments c JOIN members m ON m.id=c.member_id WHERE c.song_id=? ORDER BY c.created_at""", (row["id"],))]
        result["practice_history"] = [dict(r) for r in conn.execute("""SELECT r.id AS rehearsal_id,r.title,r.start_utc,r.status,rs.notes
             FROM rehearsal_songs rs JOIN rehearsals r ON r.id=rs.rehearsal_id WHERE rs.song_id=? ORDER BY r.start_utc DESC""", (row["id"],))]
    return result


def fetch_availability(conn, band_id: int, start_day: date, end_day: date) -> list[dict]:
    rows = conn.execute("""SELECT a.*,m.display_name,m.color,m.role FROM availability a
      JOIN members m ON m.id=a.member_id WHERE a.band_id=? AND m.active=1 AND a.start_date<=?
      AND ((a.recurrence='weekly' AND (a.until_date IS NULL OR a.until_date>=?))
        OR (a.recurrence='none' AND a.start_date>=?))""", (band_id, end_day.isoformat(), start_day.isoformat(), start_day.isoformat())).fetchall()
    entries = []
    for row in rows:
        item = dict(row)
        item["exceptions"] = [r[0] for r in conn.execute("SELECT exception_date FROM availability_exceptions WHERE availability_id=?", (row["id"],))]
        entries.append(item)
    return entries


def get_rehearsal(conn, rehearsal_id: int, band_id: int, include_conflicts: bool = True) -> dict:
    row = conn.execute("SELECT * FROM rehearsals WHERE id=? AND band_id=?", (rehearsal_id, band_id)).fetchone()
    if not row:
        raise ApiError(404, "Rehearsal not found")
    result = dict(row)
    result["invited_ids"] = parse_json_list(row["invited_ids_json"])
    result["split_member_ids"] = parse_json_list(row["split_member_ids_json"])
    result["organizer"] = dict(conn.execute("SELECT id,display_name FROM members WHERE id=?", (row["organizer_id"],)).fetchone())
    editor=conn.execute("SELECT id,display_name FROM members WHERE id=?",(row["updated_by"],)).fetchone() if row["updated_by"] else None
    result["updated_by_member"]=dict(editor) if editor else None
    result["attendance"] = [dict(r) for r in conn.execute("""SELECT a.member_id,a.response,m.display_name,m.color FROM rehearsal_attendance a
        JOIN members m ON m.id=a.member_id WHERE a.rehearsal_id=? ORDER BY m.display_name""", (rehearsal_id,))]
    result["songs"] = [dict(r) for r in conn.execute("""SELECT rs.song_id,rs.position,rs.notes,s.title,s.artist,s.duration_minutes
        FROM rehearsal_songs rs JOIN songs s ON s.id=rs.song_id WHERE rs.rehearsal_id=? ORDER BY rs.position""", (rehearsal_id,))]
    result["cost_shares"] = [dict(r) for r in conn.execute("""SELECT c.member_id,c.amount_cents,c.reimbursement_paid,m.display_name,m.color
        FROM cost_shares c JOIN members m ON m.id=c.member_id WHERE c.rehearsal_id=? ORDER BY m.display_name""", (rehearsal_id,))]
    result["cost_shares"] = [{**c, "amount_cad": c["amount_cents"] / 100, "reimbursement_paid": bool(c["reimbursement_paid"])} for c in result["cost_shares"]]
    result["total_cost_cents"] = int(round(row["total_cost_cad"] * 100))
    result["duration_minutes"] = max(0, int((datetime.fromisoformat(row["end_utc"]) - datetime.fromisoformat(row["start_utc"])).total_seconds() // 60))
    result["venue_paid_by"] = None
    if row["venue_paid_by_id"]:
        payer = conn.execute("SELECT id,display_name FROM members WHERE id=?", (row["venue_paid_by_id"],)).fetchone()
        result["venue_paid_by"] = dict(payer) if payer else None
    if include_conflicts:
        local_start = datetime.fromisoformat(row["start_utc"]).astimezone(ZoneInfo(row["timezone"]))
        local_end = datetime.fromisoformat(row["end_utc"]).astimezone(ZoneInfo(row["timezone"]))
        day = local_start.date()
        items = fetch_availability(conn, band_id, day, local_end.date())
        expanded = expand_availability(items, day, local_end.date(), row["timezone"])
        by_member = {}
        for it in expanded:
            by_member.setdefault(int(it["member_id"]), []).append(it)
        conflicts = []
        for mid in result["invited_ids"]:
            submitted = by_member.get(int(mid), [])
            if any(i.get("kind") == "unavailable" and i["date"] == day.isoformat() for i in submitted):
                conflicts.append(int(mid)); continue
            submitted_for_day = [i for i in submitted if i["date"] == day.isoformat()]
            if submitted_for_day and not any(i.get("kind", "available") == "available"
                and datetime.fromisoformat(i["start_utc"]) <= datetime.fromisoformat(row["start_utc"])
                and datetime.fromisoformat(i["end_utc"]) >= datetime.fromisoformat(row["end_utc"])
                for i in submitted_for_day):
                conflicts.append(int(mid))
        result["availability_conflict_ids"] = conflicts
    return result


def recalculate_shares(conn, rehearsal_id: int):
    row = conn.execute("SELECT * FROM rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
    if not row:
        return
    if row["split_mode"] == "attending":
        ids = [r[0] for r in conn.execute("SELECT member_id FROM rehearsal_attendance WHERE rehearsal_id=? AND response='attending' ORDER BY member_id", (rehearsal_id,))]
    elif row["split_mode"] == "active":
        ids = [r[0] for r in conn.execute("SELECT id FROM members WHERE band_id=? AND active=1 ORDER BY id", (row["band_id"],))]
    else:
        ids = validate_member_ids(conn, row["band_id"], parse_json_list(row["split_member_ids_json"]), "Cost split members")
    amounts = split_cost(int(round(row["total_cost_cad"] * 100)), ids)
    existing = {r["member_id"]: r["reimbursement_paid"] for r in conn.execute("SELECT member_id,reimbursement_paid FROM cost_shares WHERE rehearsal_id=?", (rehearsal_id,))}
    conn.execute("DELETE FROM cost_shares WHERE rehearsal_id=?", (rehearsal_id,))
    for mid, cents in amounts.items():
        conn.execute("INSERT INTO cost_shares(rehearsal_id,member_id,amount_cents,reimbursement_paid) VALUES(?,?,?,?)",
                     (rehearsal_id, mid, cents, existing.get(mid, 0)))


class Handler(BaseHTTPRequestHandler):
    server_version = "Bandroom/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("BAND_LOG", "1") != "0":
            super().log_message(fmt, *args)

    def get_cookie(self, name: str) -> str | None:
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies[name].value if name in cookies else None
        except Exception:
            return None

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if headers:
            for key, value in headers.items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(body)

    def send_error_json(self, error: ApiError):
        payload = {"error": error.message}
        if error.details is not None: payload["details"] = error.details
        self.send_json(payload, error.status)

    def body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid request size")
        if length > 1_000_000:
            raise ApiError(413, "Request is too large")
        if length == 0: return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be an object")
        return value

    def do_GET(self): self.handle_request("GET")
    def do_POST(self): self.handle_request("POST")
    def do_PATCH(self): self.handle_request("PATCH")
    def do_DELETE(self): self.handle_request("DELETE")

    def handle_request(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            self.serve_static(path)
            return
        conn = None
        try:
            body = self.body() if method in ("POST", "PATCH", "DELETE") else {}
            if path in ("/api/login", "/api/signup", "/api/join") and method == "POST":
                self.auth_route(conn, path, body); return
            if path in ("/api/password-reset/request", "/api/password-reset/complete") and method == "POST":
                self.password_reset_route(path, body); return
            if path == "/api/status" and method == "GET":
                with db_connect() as c:
                    configured = bool(c.execute("SELECT 1 FROM bands LIMIT 1").fetchone())
                self.send_json({"configured": configured}); return
            if path == "/api/logout" and method == "POST":
                token = self.get_cookie("band_session")
                if token:
                    with db_connect() as c: c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                self.send_json({"ok": True}, headers={"Set-Cookie": "band_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"}); return
            conn, member, band = require_member(self)
            self.route(conn, member, band, path, method, query, body)
        except ApiError as error:
            self.send_error_json(error)
        except DomainError as error:
            self.send_error_json(ApiError(400, str(error)))
        except (ValueError, ZoneInfoNotFoundError) as error:
            self.send_error_json(ApiError(400, str(error) or "Invalid value"))
        except Exception as error:
            if is_database_integrity_error(error):
                self.send_error_json(ApiError(409, "That change conflicts with existing band data"))
                return
            print(f"Bandroom error: {type(error).__name__}: {error}")
            self.send_error_json(ApiError(500, "Something went wrong. Please try again."))
        finally:
            if conn is not None:
                conn.close()

    def auth_route(self, unused, path, body):
        if path == "/api/signup":
            band_name = clean_text(body.get("band_name"), "Band name", 100, True)
            display = clean_text(body.get("display_name"), "Display name", 80, True)
            email = clean_text(body.get("email"), "Email", 254, True).lower()
            password = body.get("password", "")
            valid_password(password)
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email): raise ApiError(400, "Enter a valid email address")
            c = db_connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                if c.execute("SELECT 1 FROM bands WHERE lower(name)=lower(?)", (band_name,)).fetchone():
                    raise ApiError(409, "A band with that name already exists. Choose a different band name.")
                now = now_iso()
                band_id = c.execute("INSERT INTO bands(name,timezone,created_at) VALUES(?,?,?)", (band_name, "America/Toronto", now)).lastrowid
                member_id = c.execute("INSERT INTO members(band_id,display_name,email,password_hash,role,color,created_at) VALUES(?,?,?,?,?,?,?)",
                                      (band_id, display, email, password_hash(password), "admin", pick_member_color(c,band_id), now)).lastrowid
                self.create_session(c, member_id)
                c.commit()
            finally: c.close()
            return
        if path == "/api/join":
            token = clean_text(body.get("token"), "Invitation", 200, True)
            display = clean_text(body.get("display_name"), "Display name", 80, True)
            email = clean_text(body.get("email"), "Email", 254, True).lower()
            password = body.get("password", "")
            valid_password(password)
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email): raise ApiError(400, "Enter a valid email address")
            c = db_connect()
            c.execute("BEGIN IMMEDIATE")
            invite = c.execute("SELECT * FROM invitations WHERE token_hash=? AND used_at IS NULL AND expires_at>?", (hashlib.sha256(token.encode()).hexdigest(), now_iso())).fetchone()
            if not invite: c.close(); raise ApiError(400, "This invitation is invalid or has expired")
            try:
                now = now_iso()
                mid = c.execute("INSERT INTO members(band_id,display_name,email,password_hash,role,color,created_at) VALUES(?,?,?,?,?,?,?)",
                                (invite["band_id"], display, email, password_hash(password), "member", pick_member_color(c,invite["band_id"]), now)).lastrowid
                c.execute("UPDATE invitations SET used_at=? WHERE id=?", (now, invite["id"]))
                self.create_session(c, mid); c.commit()
            finally: c.close()
            return
        if path == "/api/login":
            band_name = clean_text(body.get("band_name"), "Band name", 100, True)
            email = clean_text(body.get("email"), "Email", 254, True).lower()
            password = body.get("password", "")
            c = db_connect()
            row = c.execute("""SELECT m.* FROM members m JOIN bands b ON b.id=m.band_id
                WHERE lower(m.email)=lower(?) AND lower(b.name)=lower(?) AND m.active=1""", (email, band_name)).fetchone()
            if not row or not password_ok(password, row["password_hash"]):
                c.close(); raise ApiError(401, "Band name, email, or password is incorrect")
            self.create_session(c, row["id"]); c.commit(); c.close()
            return

    def password_reset_route(self, path, body):
        generic_response = {"message": "If that account exists, a password reset link will arrive shortly."}
        if path == "/api/password-reset/request":
            settings = password_reset_email_config()
            band_name = clean_text(body.get("band_name"), "Band name", 100, True)
            email = clean_text(body.get("email"), "Email", 254, True).lower()
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
                raise ApiError(400, "Enter a valid email address")

            now = utc_now()
            now_text = now.isoformat(timespec="seconds")
            cutoff = (now - timedelta(hours=1)).isoformat(timespec="seconds")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            reset_id = None
            recipient = None
            found_band_name = None
            conn = db_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                member = conn.execute("""SELECT m.id,m.email,b.name AS band_name FROM members m
                    JOIN bands b ON b.id=m.band_id
                    WHERE lower(m.email)=lower(?) AND lower(b.name)=lower(?) AND m.active=1""",
                    (email, band_name)).fetchone()
                if member:
                    sent_recently = conn.execute("""SELECT COUNT(*) FROM password_resets
                        WHERE member_id=? AND created_at>=?""", (member["id"], cutoff)).fetchone()[0]
                    if sent_recently < 3:
                        # Keep recent rows for rate limiting, but discard old history.
                        conn.execute("DELETE FROM password_resets WHERE member_id=? AND created_at<?",
                                     (member["id"], (now - timedelta(days=2)).isoformat(timespec="seconds")))
                        expires = (now + timedelta(minutes=30)).isoformat(timespec="seconds")
                        reset_id = conn.execute("""INSERT INTO password_resets(member_id,token_hash,expires_at,created_at)
                            VALUES(?,?,?,?)""", (member["id"], token_hash, expires, now_text)).lastrowid
                        recipient, found_band_name = member["email"], member["band_name"]
                conn.commit()
            finally:
                conn.close()

            if reset_id is not None:
                # A URL fragment is not sent to the web server or included in its access logs.
                reset_url = f"{settings['public_url']}/#reset={token}"
                try:
                    send_password_reset_email(recipient, found_band_name, reset_url, settings)
                except Exception as error:
                    cleanup = db_connect()
                    try:
                        cleanup.execute("DELETE FROM password_resets WHERE id=? AND token_hash=?",
                                        (reset_id, token_hash))
                        cleanup.commit()
                    finally:
                        cleanup.close()
                    print(f"Bandroom password reset email delivery failed: {type(error).__name__}")
                    raise ApiError(503, "We could not send the reset email. Please try again later or contact the site admin.") from error
            self.send_json(generic_response)
            return

        token = clean_text(body.get("token"), "Reset link", 200, True)
        password = body.get("password", "")
        confirmation = body.get("password_confirmation", "")
        valid_password(password)
        if not isinstance(confirmation, str) or not hmac.compare_digest(password, confirmation):
            raise ApiError(400, "The new passwords do not match")

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now_text = now_iso()
        conn = db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reset = conn.execute("""SELECT r.id,r.member_id FROM password_resets r
                JOIN members m ON m.id=r.member_id
                WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND m.active=1""",
                (token_hash, now_text)).fetchone()
            if not reset:
                raise ApiError(400, "This reset link is invalid or has expired. Request a new one.")
            changed = conn.execute("""UPDATE password_resets SET used_at=?
                WHERE id=? AND used_at IS NULL AND expires_at>?""",
                (now_text, reset["id"], now_text))
            if changed.rowcount != 1:
                raise ApiError(400, "This reset link is invalid or has expired. Request a new one.")
            conn.execute("UPDATE members SET password_hash=? WHERE id=?",
                         (password_hash(password), reset["member_id"]))
            conn.execute("DELETE FROM sessions WHERE member_id=?", (reset["member_id"],))
            conn.execute("DELETE FROM password_resets WHERE member_id=? AND id<>?",
                         (reset["member_id"], reset["id"]))
            conn.commit()
        finally:
            conn.close()
        self.send_json({"ok": True})

    def create_session(self, conn, member_id):
        token = secrets.token_urlsafe(32)
        expires = (utc_now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO sessions(token_hash,member_id,expires_at,created_at) VALUES(?,?,?,?)",
                     (hashlib.sha256(token.encode()).hexdigest(), member_id, expires, now_iso()))
        secure = "; Secure" if COOKIE_SECURE else ""
        self._pending_cookie = f"band_session={token}; Max-Age={SESSION_DAYS*86400}; Path=/; HttpOnly; SameSite=Lax{secure}"
        # A session response is issued by send_json at the end of the auth route.
        self.send_json({"ok": True}, headers={"Set-Cookie": self._pending_cookie})

    def serve_static(self, path):
        if path in ("/", "/index.html", "/join"):
            file = ROOT / "public" / "index.html"
        elif path.startswith("/assets/"):
            asset = Path(path.removeprefix("/assets/")).name
            file = ROOT / "public" / asset
        else:
            self.send_error(404); return
        if not file.exists() or not file.is_file() or ROOT / "public" not in file.parents:
            self.send_error(404); return
        content_type = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(file.suffix, "text/html; charset=utf-8")
        payload = file.read_bytes()
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(payload)

    def route(self, conn, member, band, path, method, query, body):
        bid, mid = member["band_id"], member["id"]
        if path == "/api/me" and method == "GET":
            self.send_json({"member": member_public(member), "band": {"id": bid, "name": band["name"], "timezone": band["timezone"]}}); return
        if path == "/api/members" and method == "GET":
            include_inactive=query.get("manage",[""])[0]=="1"
            if include_inactive: require_admin(member)
            where="" if include_inactive else " AND active=1"
            self.send_json({"members": [member_public(r) for r in conn.execute(f"SELECT * FROM members WHERE band_id=?{where} ORDER BY display_name", (bid,))]}); return
        if path == "/api/me" and method == "PATCH":
            display=clean_text(body.get("display_name",member["display_name"]),"Display name",80,True)
            roles=body.get("roles",parse_json_list(member["roles_json"]))
            if not isinstance(roles,list) or any(role not in ROLES for role in roles):raise ApiError(400,"Choose valid band roles")
            color=clean_text(body.get("color",member["color"]),"Member color",16)
            if not re.match(r"^#[0-9a-fA-F]{6}$",color):raise ApiError(400,"Choose a valid member color")
            duplicate=conn.execute("SELECT 1 FROM members WHERE band_id=? AND active=1 AND id<>? AND lower(color)=lower(?)",(bid,mid,color)).fetchone()
            if duplicate:raise ApiError(400,"Each active member needs a distinct calendar color")
            conn.execute("UPDATE members SET display_name=?,roles_json=?,color=? WHERE id=?",(display,json.dumps(list(dict.fromkeys(roles))),color,mid));conn.commit()
            self.send_json({"member":member_public(conn.execute("SELECT * FROM members WHERE id=?",(mid,)).fetchone()),"band":{"id":bid,"name":band["name"],"timezone":band["timezone"]}});return
        member_match=re.fullmatch(r"/api/members/(\d+)",path)
        if member_match:
            require_admin(member); target=int(member_match.group(1))
            target_row=conn.execute("SELECT * FROM members WHERE id=? AND band_id=?",(target,bid)).fetchone()
            if not target_row:raise ApiError(404,"Member not found")
            display=clean_text(body.get("display_name",target_row["display_name"]),"Display name",80,True)
            role=body.get("role",target_row["role"])
            if role not in ("admin","member"):raise ApiError(400,"Choose admin or member")
            active=bool(body.get("active",target_row["active"]))
            roles=body.get("roles",parse_json_list(target_row["roles_json"]))
            if not isinstance(roles,list) or any(value not in ROLES for value in roles):raise ApiError(400,"Choose valid band roles")
            if not active and target==mid:raise ApiError(400,"You cannot deactivate your own account")
            if target_row["role"]=="admin" and (not active or role!="admin"):
                count=conn.execute("SELECT COUNT(*) FROM members WHERE band_id=? AND active=1 AND role='admin'",(bid,)).fetchone()[0]
                if count<=1:raise ApiError(400,"The band needs at least one active admin")
            color=target_row["color"]
            if active and not target_row["active"] and conn.execute("SELECT 1 FROM members WHERE band_id=? AND active=1 AND lower(color)=lower(?)",(bid,color)).fetchone():
                color=pick_member_color(conn,bid)
            conn.execute("UPDATE members SET display_name=?,role=?,active=?,roles_json=?,color=? WHERE id=?",(display,role,int(active),json.dumps(list(dict.fromkeys(roles))),color,target));conn.commit()
            self.send_json({"member":member_public(conn.execute("SELECT * FROM members WHERE id=?",(target,)).fetchone())});return
        if path == "/api/invitations" and method == "POST":
            require_admin(member)
            token = secrets.token_urlsafe(28); expires = utc_now() + timedelta(days=7)
            conn.execute("INSERT INTO invitations(band_id,token_hash,created_by,expires_at,created_at) VALUES(?,?,?,?,?)",
                         (bid, hashlib.sha256(token.encode()).hexdigest(), mid, expires.isoformat(timespec="seconds"), now_iso()))
            conn.commit();self.send_json({"invite_url": f"/join?token={token}", "expires_at": expires.isoformat(timespec="seconds")}, 201); return
        if path == "/api/dashboard" and method == "GET":
            self.dashboard(conn, member, band); return
        if path == "/api/announcements" and method == "POST":
            announcement = clean_text(body.get("body"), "Announcement", 2000, True)
            created_at = now_iso()
            announcement_id = conn.execute(
                "INSERT INTO announcements(band_id,member_id,body,created_at) VALUES(?,?,?,?)",
                (bid, mid, announcement, created_at),
            ).lastrowid
            record_activity(conn, bid, mid, "posted an announcement", "announcement", announcement_id,
                            announcement[:120])
            conn.commit()
            saved = conn.execute("""SELECT a.id,a.body,a.created_at,m.id AS member_id,m.display_name,m.color
                FROM announcements a JOIN members m ON m.id=a.member_id
                WHERE a.id=? AND a.band_id=?""", (announcement_id, bid)).fetchone()
            self.send_json(dict(saved), 201); return
        if path == "/api/songs" and method == "GET":
            self.list_songs(conn, member, query); return
        if path == "/api/songs" and method == "POST":
            data = normalize_song(conn, bid, body)
            now = now_iso()
            song_id = conn.execute("""INSERT INTO songs(band_id,title,artist,status,performed_live,performed_dates_json,singer_ids_json,pianist_id,
                key_name,bpm,duration_minutes,difficulty,priority,tags_json,links_json,arrangement_notes,practice_notes,created_by,updated_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid,data["title"],data["artist"],data["status"],int(data["performed_live"]),data["performed_dates_json"],data["singer_ids_json"],data["pianist_id"],
                 data["key_name"],data["bpm"],data["duration_minutes"],data["difficulty"],data["priority"],data["tags_json"],data["links_json"],data["arrangement_notes"],data["practice_notes"],mid,mid,now,now)).lastrowid
            record_activity(conn,bid,mid,"added a song","song",song_id,data["title"]); conn.commit()
            self.send_json(song_object(conn, conn.execute("SELECT s.*,m.display_name AS updated_by_name FROM songs s LEFT JOIN members m ON m.id=s.updated_by WHERE s.id=?", (song_id,)).fetchone(), True),201); return
        match = re.fullmatch(r"/api/songs/(\d+)(?:/(comments|vote))?", path)
        if match:
            song_id = int(match.group(1)); action = match.group(2)
            row = conn.execute("SELECT s.*,m.display_name AS updated_by_name,EXISTS(SELECT 1 FROM song_votes v WHERE v.song_id=s.id AND v.member_id=?) AS member_voted FROM songs s LEFT JOIN members m ON m.id=s.updated_by WHERE s.id=? AND s.band_id=?", (mid,song_id,bid)).fetchone()
            if not row: raise ApiError(404,"Song not found")
            if action == "comments" and method == "POST":
                text = clean_text(body.get("body"),"Comment",2000,True)
                cid = conn.execute("INSERT INTO song_comments(song_id,member_id,body,created_at) VALUES(?,?,?,?)",(song_id,mid,text,now_iso())).lastrowid
                record_activity(conn,bid,mid,"commented on","song",song_id,row["title"]); conn.commit()
                comment = conn.execute("SELECT c.id,c.body,c.created_at,m.id AS member_id,m.display_name,m.color FROM song_comments c JOIN members m ON m.id=c.member_id WHERE c.id=?",(cid,)).fetchone()
                self.send_json(dict(comment),201); return
            if action == "vote" and method == "POST":
                exists = conn.execute("SELECT 1 FROM song_votes WHERE song_id=? AND member_id=?",(song_id,mid)).fetchone()
                if exists: conn.execute("DELETE FROM song_votes WHERE song_id=? AND member_id=?",(song_id,mid)); voted=False
                else: conn.execute("INSERT INTO song_votes(song_id,member_id,value,created_at) VALUES(?,?,1,?)",(song_id,mid,now_iso())); voted=True
                conn.commit(); self.send_json({"voted":voted,"votes":conn.execute("SELECT COUNT(*) FROM song_votes WHERE song_id=?",(song_id,)).fetchone()[0]}); return
            if action is not None: raise ApiError(405,"Method not allowed")
            if method == "GET": self.send_json(song_object(conn,row,True)); return
            if method == "PATCH":
                expected = body.get("version")
                if expected is None: raise ApiError(400,"Song version is required to edit")
                if int(expected) != row["version"]: raise ApiError(409,"This song changed since you opened it. Reload the latest version before saving.",song_object(conn,row,True))
                previous = dict(row); previous.update({"performed_dates":parse_json_list(row["performed_dates_json"]),"singer_ids":parse_json_list(row["singer_ids_json"]),"tags":parse_json_list(row["tags_json"]),"links":parse_json_list(row["links_json"])})
                normalized = normalize_song(conn,bid,body,previous); now=now_iso()
                conn.execute("""UPDATE songs SET title=?,artist=?,status=?,performed_live=?,performed_dates_json=?,singer_ids_json=?,pianist_id=?,key_name=?,bpm=?,duration_minutes=?,difficulty=?,priority=?,tags_json=?,links_json=?,arrangement_notes=?,practice_notes=?,updated_by=?,updated_at=?,version=version+1 WHERE id=? AND version=?""",
                    (normalized["title"],normalized["artist"],normalized["status"],int(normalized["performed_live"]),normalized["performed_dates_json"],normalized["singer_ids_json"],normalized["pianist_id"],normalized["key_name"],normalized["bpm"],normalized["duration_minutes"],normalized["difficulty"],normalized["priority"],normalized["tags_json"],normalized["links_json"],normalized["arrangement_notes"],normalized["practice_notes"],mid,now,song_id,int(expected)))
                if conn.total_changes == 0: raise ApiError(409,"This song changed before your edit was saved. Reload and try again.")
                record_activity(conn,bid,mid,"updated a song","song",song_id,normalized["title"]); conn.commit()
                out=conn.execute("SELECT s.*,m.display_name AS updated_by_name,EXISTS(SELECT 1 FROM song_votes v WHERE v.song_id=s.id AND v.member_id=?) AS member_voted FROM songs s JOIN members m ON m.id=s.updated_by WHERE s.id=?",(mid,song_id)).fetchone()
                self.send_json(song_object(conn,out,True)); return
            if method == "DELETE":
                if body.get("confirm") is not True: raise ApiError(400,"Confirm song deletion")
                if body.get("version") is None:raise ApiError(400,"Song version is required to delete")
                if int(body["version"])!=row["version"]:raise ApiError(409,"This song changed since you opened it. Reload before deleting.",song_object(conn,row,True))
                deleted=conn.execute("DELETE FROM songs WHERE id=? AND band_id=? AND version=?",(song_id,bid,int(body["version"])))
                if deleted.rowcount==0:raise ApiError(409,"This song changed while you were deleting it. Reload before deleting.")
                conn.commit(); self.send_json({"ok":True}); return
            raise ApiError(405,"Method not allowed")
        if path == "/api/availability" and method == "GET": self.list_availability(conn,member,band,query); return
        if path == "/api/availability" and method == "POST": self.create_availability(conn,member,band,body); return
        match = re.fullmatch(r"/api/availability/(\d+)(?:/exceptions)?",path)
        if match:
            availability_id=int(match.group(1)); is_exception=path.endswith("/exceptions")
            availability=conn.execute("SELECT * FROM availability WHERE id=? AND band_id=?",(availability_id,bid)).fetchone()
            if not availability: raise ApiError(404,"Availability entry not found")
            if availability["member_id"] != mid: require_admin(member)
            if is_exception:
                exception_date=clean_text(body.get("date") or query.get("date",[""])[0],"Exception date",10,True)
                try: date.fromisoformat(exception_date)
                except ValueError: raise ApiError(400,"Exception date must be valid")
                if method=="POST":
                    if availability["recurrence"]!="weekly": raise ApiError(400,"Only recurring availability can have exceptions")
                    exception_day=date.fromisoformat(exception_date)
                    first_day=date.fromisoformat(availability["start_date"])
                    last_day=date.fromisoformat(availability["until_date"]) if availability["until_date"] else None
                    if exception_day<first_day or (last_day and exception_day>last_day) or exception_day.weekday()!=first_day.weekday():
                        raise ApiError(400,"Choose a date covered by this weekly recurrence")
                    changed=conn.execute("INSERT OR IGNORE INTO availability_exceptions(availability_id,exception_date,created_at) VALUES(?,?,?)",(availability_id,exception_date,now_iso()))
                    if changed.rowcount:conn.execute("UPDATE availability SET version=version+1 WHERE id=?",(availability_id,))
                elif method=="DELETE":
                    changed=conn.execute("DELETE FROM availability_exceptions WHERE availability_id=? AND exception_date=?",(availability_id,exception_date))
                    if changed.rowcount:conn.execute("UPDATE availability SET version=version+1 WHERE id=?",(availability_id,))
                else: raise ApiError(405,"Method not allowed")
                conn.commit(); self.send_json({"ok":True}); return
            if method=="PATCH":
                if body.get("version") is None:raise ApiError(400,"Availability version is required to edit")
                if int(body["version"])!=availability["version"]:raise ApiError(409,"This availability entry changed since you opened it. Reload before saving.")
                kind=body.get("kind",availability["kind"]);recurrence=body.get("recurrence",availability["recurrence"])
                if kind not in ("available","unavailable") or recurrence not in ("none","weekly"):raise ApiError(400,"Choose a valid availability type")
                try:start_day=date.fromisoformat(body.get("start_date",availability["start_date"]))
                except ValueError:raise ApiError(400,"Choose a valid date")
                until=None
                if recurrence=="weekly" and body.get("until_date"):
                    try:until=date.fromisoformat(body["until_date"])
                    except ValueError:raise ApiError(400,"Recurring availability end date must be valid")
                    if until<start_day or (until-start_day).days>366:raise ApiError(400,"Recurring end date must be within one year and after its start")
                st=et=None;sf=ef=0
                if kind=="available":
                    try:
                        st=time.fromisoformat(body.get("start_time",availability["start_time"]));et=time.fromisoformat(body.get("end_time",availability["end_time"]))
                        sf=int(body.get("start_fold",availability["start_fold"]));ef=int(body.get("end_fold",availability["end_fold"]))
                        if sf not in (0,1) or ef not in (0,1):raise ValueError()
                        utc_interval(start_day,st,et,band["timezone"],sf,ef)
                    except (ValueError,DomainError) as error:raise ApiError(400,str(error) or "Enter a valid time range")
                    st=st.strftime("%H:%M");et=et.strftime("%H:%M")
                note=clean_text(body.get("note",availability["note"]),"Availability note",500)
                updated=conn.execute("""UPDATE availability SET kind=?,start_date=?,start_time=?,end_time=?,recurrence=?,until_date=?,start_fold=?,end_fold=?,note=?,version=version+1
                    WHERE id=? AND version=?""",(kind,start_day.isoformat(),st,et,recurrence,until.isoformat() if until else None,sf,ef,note,availability_id,int(body["version"])))
                if updated.rowcount==0:raise ApiError(409,"This availability entry changed while you were editing it. Reload before saving.")
                conn.commit();self.send_json({"ok":True});return
            if method=="DELETE":
                if body.get("version") is None:raise ApiError(400,"Availability version is required to delete")
                if int(body["version"])!=availability["version"]:raise ApiError(409,"This availability entry changed since you opened it. Reload before removing it.")
                deleted=conn.execute("DELETE FROM availability WHERE id=? AND version=?",(availability_id,int(body["version"])))
                if deleted.rowcount==0:raise ApiError(409,"This availability entry changed while you were removing it.")
                conn.commit(); self.send_json({"ok":True}); return
            raise ApiError(405,"Method not allowed")
        if path == "/api/overlaps" and method == "GET": self.overlaps(conn,member,band,query); return
        if path == "/api/locations" and method == "GET": self.list_locations(conn,bid); return
        if path == "/api/locations" and method == "POST": self.create_location(conn,member,body); return
        match=re.fullmatch(r"/api/locations/(\d+)",path)
        if match:
            if method=="PATCH": self.update_location(conn,member,int(match.group(1)),body); return
            if method=="DELETE":
                row=conn.execute("SELECT id FROM locations WHERE id=? AND band_id=?",(int(match.group(1)),bid)).fetchone()
                if not row: raise ApiError(404,"Location not found")
                if body.get("confirm") is not True: raise ApiError(400,"Confirm location deletion")
                version=conn.execute("SELECT version FROM locations WHERE id=?",(int(match.group(1)),)).fetchone()[0]
                if body.get("version") is None:raise ApiError(400,"Location version is required to delete")
                if int(body["version"])!=version:raise ApiError(409,"This location changed since you opened it. Reload before deleting.")
                deleted=conn.execute("DELETE FROM locations WHERE id=? AND version=?",(int(match.group(1)),int(body["version"])))
                if deleted.rowcount==0:raise ApiError(409,"This location changed while you were deleting it. Reload before deleting.")
                conn.commit(); self.send_json({"ok":True}); return
        if path == "/api/rehearsals" and method == "GET": self.list_rehearsals(conn,bid,query); return
        if path == "/api/rehearsals" and method == "POST": self.create_rehearsal(conn,member,band,body); return
        match=re.fullmatch(r"/api/rehearsals/(\d+)(?:/(response|songs|shares))?",path)
        if match:
            rid=int(match.group(1)); action=match.group(2)
            rehearsal=conn.execute("SELECT * FROM rehearsals WHERE id=? AND band_id=?",(rid,bid)).fetchone()
            if not rehearsal: raise ApiError(404,"Rehearsal not found")
            if action=="response" and method=="POST":
                target=body.get("member_id",mid)
                if int(target)!=mid: require_admin(member)
                self.set_attendance(conn,member,rid,int(target),body); return
            if action=="songs":
                if method in ("POST","DELETE"):
                    if body.get("version") is None:raise ApiError(400,"Rehearsal version is required to edit its setlist")
                    if int(body["version"])!=rehearsal["version"]:raise ApiError(409,"This setlist changed since you opened it. Reload before saving.",get_rehearsal(conn,rid,bid))
                if method=="POST": self.add_rehearsal_song(conn,member,rid,body); return
                if method=="DELETE":
                    sid=int(body.get("song_id",0)); conn.execute("DELETE FROM rehearsal_songs WHERE rehearsal_id=? AND song_id=?",(rid,sid)); self.reposition_setlist(conn,rid)
                    changed=conn.execute("UPDATE rehearsals SET updated_by=?,updated_at=?,version=version+1 WHERE id=? AND version=?",(mid,now_iso(),rid,int(body["version"])))
                    if changed.rowcount==0:raise ApiError(409,"This setlist changed while you were editing it. Reload before saving.",get_rehearsal(conn,rid,bid))
                    conn.commit(); self.send_json({"ok":True}); return
            if action=="shares" and method=="PATCH": self.update_share(conn,member,rid,body); return
            if action: raise ApiError(405,"Method not allowed")
            if method=="GET": self.send_json(get_rehearsal(conn,rid,bid)); return
            if method=="PATCH": self.update_rehearsal(conn,member,band,rehearsal,body); return
            if method=="DELETE":
                if body.get("confirm") is not True: raise ApiError(400,"Confirm rehearsal deletion")
                if body.get("version") is None:raise ApiError(400,"Rehearsal version is required to delete")
                if int(body["version"])!=rehearsal["version"]:raise ApiError(409,"This rehearsal changed since you opened it. Reload before deleting.",get_rehearsal(conn,rid,bid))
                deleted=conn.execute("DELETE FROM rehearsals WHERE id=? AND version=?",(rid,int(body["version"])))
                if deleted.rowcount==0:raise ApiError(409,"This rehearsal changed while you were deleting it. Reload before deleting.")
                conn.commit(); self.send_json({"ok":True}); return
        raise ApiError(404,"Not found")

    def dashboard(self, conn, member, band):
        bid, mid = member["band_id"], member["id"]
        now = utc_now().isoformat(timespec="seconds")
        upcoming = conn.execute("SELECT id FROM rehearsals WHERE band_id=? AND status IN ('proposed','confirmed') AND start_utc>=? ORDER BY start_utc LIMIT 1",(bid,now)).fetchone()
        next_rehearsal = get_rehearsal(conn,upcoming["id"],bid) if upcoming else None
        songs=[]
        if upcoming:
            songs=[dict(r) for r in conn.execute("SELECT s.id,s.title,s.artist,s.status,s.duration_minutes,rs.position FROM rehearsal_songs rs JOIN songs s ON s.id=rs.song_id WHERE rs.rehearsal_id=? ORDER BY rs.position",(upcoming["id"],))]
        practice=[song_object(conn,r) for r in conn.execute("""SELECT s.*,m.display_name AS updated_by_name,EXISTS(SELECT 1 FROM song_votes v WHERE v.song_id=s.id AND v.member_id=?) AS member_voted
            FROM songs s JOIN members m ON m.id=s.updated_by WHERE s.band_id=? AND s.status IN ('learning','practicing') ORDER BY s.priority DESC,s.updated_at DESC LIMIT 5""",(mid,bid))]
        updates=[dict(r) for r in conn.execute("""SELECT a.id,a.action,a.entity_type,a.entity_title,a.created_at,m.display_name,m.color FROM activity a
            JOIN members m ON m.id=a.member_id WHERE a.band_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 8""",(bid,))]
        announcements=[dict(r) for r in conn.execute("""SELECT a.id,a.body,a.created_at,m.id AS member_id,m.display_name,m.color
            FROM announcements a JOIN members m ON m.id=a.member_id WHERE a.band_id=?
            ORDER BY a.created_at DESC,a.id DESC LIMIT 10""",(bid,))]
        self.send_json({"next_rehearsal":next_rehearsal,"planned_songs":songs,"needs_practice":practice,"recent_updates":updates,
                        "announcements":announcements,
                        "songs_count":conn.execute("SELECT COUNT(*) FROM songs WHERE band_id=? AND status!='archived'",(bid,)).fetchone()[0],
                        "member_count":conn.execute("SELECT COUNT(*) FROM members WHERE band_id=? AND active=1",(bid,)).fetchone()[0]})

    def list_songs(self, conn, member, query):
        bid, mid = member["band_id"],member["id"]
        search=query.get("q",[""])[0].strip()
        sql="""SELECT s.*,m.display_name AS updated_by_name,EXISTS(SELECT 1 FROM song_votes v WHERE v.song_id=s.id AND v.member_id=?) AS member_voted
             FROM songs s LEFT JOIN members m ON m.id=s.updated_by WHERE s.band_id=?"""
        args=[mid,bid]
        status=query.get("status",[""])[0]
        if status:
            if status not in STATUSES: raise ApiError(400,"Invalid status filter")
            sql+=" AND s.status=?";args.append(status)
        if search:
            sql+=" AND (s.title LIKE ? OR s.artist LIKE ?)";args.extend([f"%{search}%",f"%{search}%"])
        if query.get("singer",[""])[0]:
            try: sid=int(query["singer"][0])
            except ValueError: raise ApiError(400,"Invalid singer filter")
            sql+=" AND EXISTS(SELECT 1 FROM json_each(s.singer_ids_json) WHERE CAST(value AS INTEGER)=?)";args.append(sid)
        if query.get("pianist",[""])[0]:
            try: pid=int(query["pianist"][0])
            except ValueError: raise ApiError(400,"Invalid pianist filter")
            sql+=" AND s.pianist_id=?";args.append(pid)
        if query.get("priority",[""])[0]:
            try: priority=int(query["priority"][0])
            except ValueError: raise ApiError(400,"Invalid priority filter")
            sql+=" AND s.priority=?";args.append(priority)
        if query.get("tag",[""])[0]:
            sql+=" AND EXISTS(SELECT 1 FROM json_each(s.tags_json) WHERE lower(value)=lower(?))";args.append(query["tag"][0])
        sort=query.get("sort",["priority"])[0]
        order={"title":"lower(s.title),lower(s.artist)","added":"s.created_at DESC","priority":"s.priority DESC,lower(s.title)","practiced":"(SELECT MAX(r.start_utc) FROM rehearsal_songs rs JOIN rehearsals r ON r.id=rs.rehearsal_id WHERE rs.song_id=s.id AND r.status='completed') DESC,lower(s.title)"}.get(sort)
        if not order: raise ApiError(400,"Invalid sort order")
        sql+=" ORDER BY "+order+" LIMIT 500"
        self.send_json({"songs":[song_object(conn,r) for r in conn.execute(sql,args)]})

    def list_availability(self, conn, member, band, query):
        try:
            start_day=date.fromisoformat(query.get("from",[date.today().isoformat()])[0])
            end_day=date.fromisoformat(query.get("to",[(start_day+timedelta(days=6)).isoformat()])[0])
        except ValueError: raise ApiError(400,"Use valid from and to dates")
        if end_day<start_day or (end_day-start_day).days>92: raise ApiError(400,"Choose a date range of 1 to 93 days")
        records=fetch_availability(conn,member["band_id"],start_day,end_day)
        expanded=expand_availability(records,start_day,end_day,band["timezone"])
        for item in expanded:
            item["member_id"]=int(item["member_id"])
            item["is_mine"]=item["member_id"]==member["id"]
        self.send_json({"entries":expanded,"members":[member_public(r) for r in conn.execute("SELECT * FROM members WHERE band_id=? AND active=1 ORDER BY display_name",(member["band_id"],))],"timezone":band["timezone"]})

    def create_availability(self, conn, member, band, body):
        kind=body.get("kind","available")
        recurrence=body.get("recurrence","none")
        if kind not in ("available","unavailable"): raise ApiError(400,"Choose available or unavailable")
        if recurrence not in ("none","weekly"): raise ApiError(400,"Choose a valid recurrence")
        try: start_day=date.fromisoformat(body.get("start_date",""))
        except ValueError: raise ApiError(400,"Choose a valid date")
        until=None
        if recurrence=="weekly" and body.get("until_date"):
            try: until=date.fromisoformat(body["until_date"])
            except ValueError: raise ApiError(400,"Recurring availability end date must be valid")
            if until<start_day: raise ApiError(400,"Recurring availability end date must be on or after its first date")
            if (until-start_day).days>366: raise ApiError(400,"Recurring availability can be set for up to one year")
        st=et=None; sf=ef=0
        if kind=="available":
            try:
                st=time.fromisoformat(body.get("start_time","")); et=time.fromisoformat(body.get("end_time",""))
                sf=int(body.get("start_fold",0));ef=int(body.get("end_fold",0))
                if sf not in (0,1) or ef not in (0,1): raise ValueError()
                utc_interval(start_day,st,et,band["timezone"],sf,ef)
            except (ValueError,DomainError) as error: raise ApiError(400,str(error) or "Enter a valid time range")
            st=st.strftime("%H:%M");et=et.strftime("%H:%M")
        note=clean_text(body.get("note"),"Availability note",500)
        entry_id=conn.execute("""INSERT INTO availability(band_id,member_id,kind,start_date,start_time,end_time,recurrence,until_date,start_fold,end_fold,note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(member["band_id"],member["id"],kind,start_day.isoformat(),st,et,recurrence,until.isoformat() if until else None,sf,ef,note,now_iso())).lastrowid
        conn.commit(); self.send_json({"id":entry_id,"ok":True},201)

    def overlaps(self, conn, member, band, query):
        try:
            start_day=date.fromisoformat(query.get("from",[""])[0]);end_day=date.fromisoformat(query.get("to",[""])[0])
            minimum=int(query.get("min_minutes",["120"])[0])
        except ValueError: raise ApiError(400,"Choose valid dates and a rehearsal duration")
        if end_day<start_day or (end_day-start_day).days>93: raise ApiError(400,"Choose a date range of 1 to 94 days")
        if minimum<30 or minimum>720: raise ApiError(400,"Minimum duration must be between 30 and 720 minutes")
        ids_raw=query.get("members",[""])[0]
        if ids_raw:
            try: ids=[int(x) for x in ids_raw.split(",") if x]
            except ValueError: raise ApiError(400,"Invalid member selection")
            ids=validate_member_ids(conn,member["band_id"],ids,"Required members")
        else: ids=[r[0] for r in conn.execute("SELECT id FROM members WHERE band_id=? AND active=1 ORDER BY id",(member["band_id"],))]
        records=fetch_availability(conn,member["band_id"],start_day,end_day)
        expanded=expand_availability(records,start_day,end_day,band["timezone"])
        slots=availability_suggestions(expanded,ids,start_day,end_day,minimum,band["timezone"])
        people=[member_public(r) for r in conn.execute("SELECT * FROM members WHERE band_id=? AND active=1 ORDER BY display_name",(member["band_id"],)) if r["id"] in ids]
        fully=[s for s in slots if s["available_count"]==len(ids) and ids]
        self.send_json({"slots":slots[:100],"full_overlap_count":len(fully),"required_member_ids":ids,"members":people,"timezone":band["timezone"]})

    def list_locations(self, conn, band_id):
        rows=conn.execute("SELECT * FROM locations WHERE band_id=? ORDER BY name",(band_id,)).fetchall()
        self.send_json({"locations":[{**dict(r),"equipment":parse_json_list(r["equipment_json"])} for r in rows]})

    def location_data(self, body, current=None):
        data={**(current or {}),**body}
        name=clean_text(data.get("name"),"Venue name",120,True)
        address=clean_text(data.get("address"),"Address",400)
        def money(key,label):
            value=data.get(key)
            if value in (None,""): return None
            try: value=float(value)
            except (TypeError,ValueError): raise ApiError(400,f"{label} must be a number")
            if not 0<=value<=100000: raise ApiError(400,f"{label} must be between 0 and 100000 CAD")
            return value
        equipment=[clean_text(x,"Equipment",40) for x in validate_json_list(data.get("equipment",[]),"Equipment",30)]
        return {"name":name,"address":address,"map_url":safe_url(data.get("map_url"),"Map link"),
                "hourly_rate":money("hourly_rate","Hourly rate"),"flat_cost":money("flat_cost","Flat cost"),
                "contact":clean_text(data.get("contact"),"Contact",200),"booking_link":safe_url(data.get("booking_link"),"Booking link"),
                "equipment_json":json.dumps(equipment),"notes":clean_text(data.get("notes"),"Location notes",2000)}

    def create_location(self, conn, member, body):
        data=self.location_data(body);now=now_iso()
        lid=conn.execute("""INSERT INTO locations(band_id,name,address,map_url,hourly_rate,flat_cost,contact,booking_link,equipment_json,notes,created_by,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(member["band_id"],data["name"],data["address"],data["map_url"],data["hourly_rate"],data["flat_cost"],data["contact"],data["booking_link"],data["equipment_json"],data["notes"],member["id"],now)).lastrowid
        conn.commit();self.send_json({"id":lid,"ok":True},201)

    def update_location(self, conn, member, lid, body):
        current=conn.execute("SELECT * FROM locations WHERE id=? AND band_id=?",(lid,member["band_id"])).fetchone()
        if not current: raise ApiError(404,"Location not found")
        if body.get("version") is None:raise ApiError(400,"Location version is required to edit")
        if int(body["version"])!=current["version"]:raise ApiError(409,"This location changed since you opened it. Reload before saving.")
        data=self.location_data(body,{**dict(current),"equipment":parse_json_list(current["equipment_json"])})
        updated=conn.execute("""UPDATE locations SET name=?,address=?,map_url=?,hourly_rate=?,flat_cost=?,contact=?,booking_link=?,equipment_json=?,notes=?,updated_at=?,version=version+1 WHERE id=? AND version=?""",
            (data["name"],data["address"],data["map_url"],data["hourly_rate"],data["flat_cost"],data["contact"],data["booking_link"],data["equipment_json"],data["notes"],now_iso(),lid,int(body["version"])))
        if updated.rowcount==0:raise ApiError(409,"This location changed while you were editing it. Reload before saving.")
        conn.commit();self.send_json({"ok":True})

    def list_rehearsals(self, conn, band_id, query):
        start=query.get("from",[None])[0]
        if start:
            try: start_utc=datetime.fromisoformat(start).astimezone(timezone.utc).isoformat()
            except ValueError: raise ApiError(400,"Invalid rehearsal date")
            rows=conn.execute("SELECT id FROM rehearsals WHERE band_id=? AND start_utc>=? ORDER BY start_utc LIMIT 100",(band_id,start_utc)).fetchall()
        else: rows=conn.execute("SELECT id FROM rehearsals WHERE band_id=? ORDER BY start_utc DESC LIMIT 100",(band_id,)).fetchall()
        self.send_json({"rehearsals":[get_rehearsal(conn,r["id"],band_id) for r in rows]})

    def create_rehearsal(self, conn, member, band, body):
        title=clean_text(body.get("title"),"Rehearsal title",140,True)
        event_type=body.get("event_type","rehearsal")
        if event_type not in ("rehearsal","performance"):raise ApiError(400,"Choose rehearsal or performance")
        day_text=body.get("date","")
        try:
            day=date.fromisoformat(day_text);st=time.fromisoformat(body.get("start_time",""));et=time.fromisoformat(body.get("end_time",""))
            sf=int(body.get("start_fold",0));ef=int(body.get("end_fold",0))
            left,right=utc_interval(day,st,et,band["timezone"],sf,ef)
        except (ValueError,DomainError) as error: raise ApiError(400,str(error) or "Enter a valid rehearsal time")
        status=body.get("status","proposed")
        if status not in ("proposed","confirmed"): raise ApiError(400,"New rehearsals must be proposed or confirmed")
        invited=validate_member_ids(conn,member["band_id"],body.get("invited_ids",[r[0] for r in conn.execute("SELECT id FROM members WHERE band_id=? AND active=1",(member["band_id"],))]),"Invited members")
        location_id=body.get("location_id") or None; location=None
        if location_id:
            try: location_id=int(location_id)
            except (TypeError,ValueError): raise ApiError(400,"Choose a valid saved location")
            location=conn.execute("SELECT * FROM locations WHERE id=? AND band_id=?",(location_id,member["band_id"])).fetchone()
            if not location: raise ApiError(400,"Choose a valid saved location")
        duration=int((right-left).total_seconds()//60)
        cost_override=0
        if body.get("total_cost_cad") not in (None,""):
            try: total=float(body["total_cost_cad"])
            except (TypeError,ValueError): raise ApiError(400,"Total cost must be a number")
            if not 0<=total<=100000: raise ApiError(400,"Total cost must be between 0 and 100000 CAD")
            cost_override=1
        elif location: total=estimated_cost_cents(duration,location["hourly_rate"],location["flat_cost"])/100
        else: total=0.0
        split_mode=body.get("split_mode","attending")
        if split_mode not in ("attending","active","selected"): raise ApiError(400,"Choose a valid cost split")
        split_ids=validate_member_ids(conn,member["band_id"],body.get("split_member_ids",[]),"Cost split members")
        if split_mode=="selected" and not split_ids: raise ApiError(400,"Select at least one person for a manual cost split")
        now=now_iso()
        rid=conn.execute("""INSERT INTO rehearsals(band_id,title,event_type,start_utc,end_utc,timezone,status,organizer_id,invited_ids_json,location_id,
            location_name_snapshot,address_snapshot,room_number,booking_link,booking_reference,total_cost_cad,split_mode,split_member_ids_json,cost_override,hourly_rate_snapshot,flat_cost_snapshot,goals,notes,created_at,updated_at,updated_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(member["band_id"],title,event_type,left.isoformat(),right.isoformat(),band["timezone"],status,member["id"],json.dumps(invited),location_id,
              location["name"] if location else clean_text(body.get("location_name"),"Location name",120),location["address"] if location else clean_text(body.get("address"),"Address",400),
              clean_text(body.get("room_number"),"Room number",80),safe_url(body.get("booking_link"),"Booking link"),clean_text(body.get("booking_reference"),"Booking reference",120),total,split_mode,json.dumps(split_ids),cost_override,location["hourly_rate"] if location else None,location["flat_cost"] if location else None,clean_text(body.get("goals"),"Rehearsal goals",3000),clean_text(body.get("notes"),"Rehearsal notes",5000),now,now,member["id"])).lastrowid
        for target in invited:
            conn.execute("INSERT INTO rehearsal_attendance(rehearsal_id,member_id,response) VALUES(?,?,?)",(rid,target,"no_response"))
        # A proposed invite can include an attendance response map when planned from an overlap.
        for target,response in (body.get("responses",{}) or {}).items():
            target=int(target)
            if target in invited and response in ATTENDANCE:
                conn.execute("UPDATE rehearsal_attendance SET response=? WHERE rehearsal_id=? AND member_id=?",(response,rid,target))
        recalculate_shares(conn,rid)
        record_activity(conn,member["band_id"],member["id"],"planned a rehearsal","rehearsal",rid,title);conn.commit();self.send_json(get_rehearsal(conn,rid,member["band_id"]),201)

    def set_attendance(self,conn,member,rid,target,body):
        response=body.get("response")
        if response not in ATTENDANCE: raise ApiError(400,"Choose attending, maybe, unavailable, or no response")
        exists=conn.execute("SELECT 1 FROM rehearsal_attendance WHERE rehearsal_id=? AND member_id=?",(rid,target)).fetchone()
        if not exists: raise ApiError(404,"Member is not invited to this rehearsal")
        conn.execute("UPDATE rehearsal_attendance SET response=? WHERE rehearsal_id=? AND member_id=?",(response,rid,target))
        recalculate_shares(conn,rid);conn.execute("UPDATE rehearsals SET updated_by=?,updated_at=?,version=version+1 WHERE id=?",(target,now_iso(),rid));conn.commit();self.send_json(get_rehearsal(conn,rid,member["band_id"]))

    def update_rehearsal(self,conn,member,band,row,body):
        if body.get("version") is None: raise ApiError(400,"Rehearsal version is required to edit")
        if int(body["version"])!=row["version"]: raise ApiError(409,"This rehearsal changed since you opened it. Reload the latest version before saving.",get_rehearsal(conn,row["id"],member["band_id"]))
        data={**dict(row),**body}
        title=clean_text(data.get("title"),"Rehearsal title",140,True)
        status=data.get("status")
        if status not in ("proposed","confirmed","completed","cancelled"): raise ApiError(400,"Choose a valid rehearsal status")
        event_type=data.get("event_type",row["event_type"])
        if event_type not in ("rehearsal","performance"):raise ApiError(400,"Choose rehearsal or performance")
        try:
            day=date.fromisoformat(data.get("date") or datetime.fromisoformat(row["start_utc"]).astimezone(ZoneInfo(band["timezone"])).date().isoformat())
            original_start=datetime.fromisoformat(row["start_utc"]).astimezone(ZoneInfo(band["timezone"]))
            original_end=datetime.fromisoformat(row["end_utc"]).astimezone(ZoneInfo(band["timezone"]))
            st=time.fromisoformat(data.get("start_time") or original_start.strftime("%H:%M"));et=time.fromisoformat(data.get("end_time") or original_end.strftime("%H:%M"))
            left,right=utc_interval(day,st,et,band["timezone"],int(data.get("start_fold",0)),int(data.get("end_fold",0)))
        except (ValueError,DomainError) as error: raise ApiError(400,str(error) or "Enter a valid rehearsal time")
        invited=validate_member_ids(conn,member["band_id"],data.get("invited_ids",parse_json_list(row["invited_ids_json"])),"Invited members")
        location_id=data.get("location_id") or None;location=None
        if location_id:
            location=conn.execute("SELECT * FROM locations WHERE id=? AND band_id=?",(int(location_id),member["band_id"])).fetchone()
            if not location: raise ApiError(400,"Choose a valid saved location")
            location_id=location["id"]
        split_mode=data.get("split_mode",row["split_mode"])
        if split_mode not in ("attending","active","selected"):raise ApiError(400,"Choose a valid cost split")
        split_ids=validate_member_ids(conn,member["band_id"],data.get("split_member_ids",parse_json_list(row["split_member_ids_json"])),"Cost split members")
        if split_mode=="selected" and not split_ids: raise ApiError(400,"Select at least one person for a manual cost split")
        location_changed=int(location_id or 0)!=int(row["location_id"] or 0)
        duration_changed=left.isoformat()!=row["start_utc"] or right.isoformat()!=row["end_utc"]
        rate_snapshot_hour=location["hourly_rate"] if location_changed and location else row["hourly_rate_snapshot"]
        rate_snapshot_flat=location["flat_cost"] if location_changed and location else row["flat_cost_snapshot"]
        if "total_cost_cad" in body:
            try: total=float(body["total_cost_cad"])
            except (TypeError,ValueError): raise ApiError(400,"Total cost must be a number")
            if not 0<=total<=100000:raise ApiError(400,"Total cost must be between 0 and 100000 CAD")
            cost_override=1
        elif (body.get("recalculate_cost") is True or location_changed and location or duration_changed and not row["cost_override"] and location):
            cost_override=0
            rate_hour=location["hourly_rate"] if body.get("recalculate_cost") is True and location else rate_snapshot_hour
            rate_flat=location["flat_cost"] if body.get("recalculate_cost") is True and location else rate_snapshot_flat
            total=estimated_cost_cents(int((right-left).total_seconds()//60),rate_hour,rate_flat)/100
        else:
            total=row["total_cost_cad"]
            cost_override=row["cost_override"]
        old_invited=set(parse_json_list(row["invited_ids_json"]));new_invited=set(invited)
        for mid in new_invited-old_invited:conn.execute("INSERT OR IGNORE INTO rehearsal_attendance(rehearsal_id,member_id,response) VALUES(?,?,?)",(row["id"],mid,"no_response"))
        for mid in old_invited-new_invited:conn.execute("DELETE FROM rehearsal_attendance WHERE rehearsal_id=? AND member_id=?",(row["id"],mid))
        updated=conn.execute("""UPDATE rehearsals SET title=?,event_type=?,start_utc=?,end_utc=?,status=?,invited_ids_json=?,location_id=?,location_name_snapshot=?,address_snapshot=?,room_number=?,booking_link=?,booking_reference=?,total_cost_cad=?,cost_override=?,hourly_rate_snapshot=?,flat_cost_snapshot=?,split_mode=?,split_member_ids_json=?,goals=?,notes=?,summary=?,updated_at=?,updated_by=?,version=version+1 WHERE id=? AND version=?""",
            (title,event_type,left.isoformat(),right.isoformat(),status,json.dumps(invited),location_id,location["name"] if location else clean_text(data.get("location_name",row["location_name_snapshot"]),"Location name",120),location["address"] if location else clean_text(data.get("address",row["address_snapshot"]),"Address",400),clean_text(data.get("room_number",row["room_number"]),"Room number",80),safe_url(data.get("booking_link",row["booking_link"]),"Booking link"),clean_text(data.get("booking_reference",row["booking_reference"]),"Booking reference",120),total,cost_override,rate_snapshot_hour,rate_snapshot_flat,split_mode,json.dumps(split_ids),clean_text(data.get("goals",row["goals"]),"Rehearsal goals",3000),clean_text(data.get("notes",row["notes"]),"Rehearsal notes",5000),clean_text(data.get("summary",row["summary"]),"Rehearsal summary",5000),now_iso(),member["id"],row["id"],int(body["version"])))
        if updated.rowcount==0:raise ApiError(409,"This rehearsal changed while you were editing it. Reload before saving.",get_rehearsal(conn,row["id"],member["band_id"]))
        recalculate_shares(conn,row["id"]);record_activity(conn,member["band_id"],member["id"],"updated a rehearsal","rehearsal",row["id"],title);conn.commit();self.send_json(get_rehearsal(conn,row["id"],member["band_id"]))

    def add_rehearsal_song(self,conn,member,rid,body):
        sid=body.get("song_id")
        try:sid=int(sid)
        except (ValueError,TypeError):raise ApiError(400,"Choose a song")
        song=conn.execute("SELECT id FROM songs WHERE id=? AND band_id=?",(sid,member["band_id"])).fetchone()
        if not song:raise ApiError(404,"Song not found")
        position=int(body.get("position") or (conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM rehearsal_songs WHERE rehearsal_id=?",(rid,)).fetchone()[0]))
        if position<1 or position>200:raise ApiError(400,"Setlist position must be 1 to 200")
        note=clean_text(body.get("notes"),"Setlist note",1000)
        conn.execute("UPDATE rehearsal_songs SET position=position+1 WHERE rehearsal_id=? AND position>=?",(rid,position))
        conn.execute("INSERT INTO rehearsal_songs(rehearsal_id,song_id,position,notes) VALUES(?,?,?,?) ON CONFLICT(rehearsal_id,song_id) DO UPDATE SET position=excluded.position,notes=excluded.notes",(rid,sid,position,note))
        self.reposition_setlist(conn,rid)
        changed=conn.execute("UPDATE rehearsals SET updated_by=?,updated_at=?,version=version+1 WHERE id=? AND version=?",(member["id"],now_iso(),rid,int(body["version"])))
        if changed.rowcount==0:raise ApiError(409,"This setlist changed while you were editing it. Reload before saving.",get_rehearsal(conn,rid,member["band_id"]))
        record_activity(conn,member["band_id"],member["id"],"updated a rehearsal setlist","rehearsal",rid,"rehearsal setlist");conn.commit();self.send_json(get_rehearsal(conn,rid,member["band_id"]))

    def reposition_setlist(self,conn,rid):
        rows=conn.execute("SELECT song_id FROM rehearsal_songs WHERE rehearsal_id=? ORDER BY position,song_id",(rid,)).fetchall()
        for index,row in enumerate(rows,1):conn.execute("UPDATE rehearsal_songs SET position=? WHERE rehearsal_id=? AND song_id=?",(index,rid,row["song_id"]))

    def update_share(self,conn,member,rid,body):
        target=int(body.get("member_id",member["id"]))
        if target!=member["id"]:require_admin(member)
        share=conn.execute("SELECT * FROM cost_shares WHERE rehearsal_id=? AND member_id=?",(rid,target)).fetchone()
        if "reimbursement_paid" in body:
            if not share:raise ApiError(404,"No cost share for this member")
            conn.execute("UPDATE cost_shares SET reimbursement_paid=? WHERE rehearsal_id=? AND member_id=?",(int(bool(body["reimbursement_paid"])),rid,target))
        if "venue_paid_by_id" in body:
            payer=body.get("venue_paid_by_id")
            if payer in (None,""):payer=None
            else:validate_member_ids(conn,member["band_id"],[int(payer)],"Venue payer");payer=int(payer)
            conn.execute("UPDATE rehearsals SET venue_paid_by_id=?,updated_by=?,updated_at=?,version=version+1 WHERE id=?",(payer,member["id"],now_iso(),rid))
        elif "reimbursement_paid" in body:
            conn.execute("UPDATE rehearsals SET updated_by=?,updated_at=?,version=version+1 WHERE id=?",(member["id"],now_iso(),rid))
        conn.commit();self.send_json(get_rehearsal(conn,rid,member["band_id"]))


def main():
    init_db()
    print(f"Bandroom running at http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()


if __name__ == "__main__":
    main()
