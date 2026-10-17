import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app


class FakeCursor:
    def __init__(self, rows=(), description=(), rowcount=1):
        self.rows = list(rows)
        self.description = [SimpleNamespace(name=name) for name in description]
        self.rowcount = rowcount
        self.executed = None

    def execute(self, sql, parameters):
        self.executed = (sql, parameters)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class FakeConnection:
    def __init__(self, cursor):
        self.result_cursor = cursor

    def cursor(self):
        return self.result_cursor


class PostgresCompatibilityTests(unittest.TestCase):
    def test_sqlite_placeholders_and_immediate_transaction_are_translated(self):
        sql, returns_id = app.PostgresConnection._prepare("UPDATE songs SET title=? WHERE id=?")
        self.assertEqual(sql, "UPDATE songs SET title=%s WHERE id=%s")
        self.assertFalse(returns_id)
        self.assertEqual(app.PostgresConnection._prepare("BEGIN IMMEDIATE"), ("BEGIN", False))

    def test_insert_ignore_becomes_postgres_conflict_handling(self):
        sql, returns_id = app.PostgresConnection._prepare(
            "INSERT OR IGNORE INTO rehearsal_attendance(rehearsal_id,member_id,response) VALUES(?,?,?)")
        self.assertEqual(sql, "INSERT INTO rehearsal_attendance(rehearsal_id,member_id,response) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING")
        self.assertFalse(returns_id)

    def test_insert_id_and_named_rows_are_preserved(self):
        cursor = FakeCursor(rows=[(27,)], description=["id"])
        connection = app.PostgresConnection(FakeConnection(cursor))
        result = connection.execute("INSERT INTO songs(title) VALUES(?)", ("New song",))
        self.assertEqual(result.lastrowid, 27)
        self.assertEqual(cursor.executed, ("INSERT INTO songs(title) VALUES(%s) RETURNING id", ("New song",)))

        announcement_cursor = FakeCursor(rows=[(28,)], description=["id"])
        announcement_connection = app.PostgresConnection(FakeConnection(announcement_cursor))
        result = announcement_connection.execute("INSERT INTO announcements(body) VALUES(?)", ("Practice moved",))
        self.assertEqual(result.lastrowid, 28)
        self.assertEqual(announcement_cursor.executed,
                         ("INSERT INTO announcements(body) VALUES(%s) RETURNING id", ("Practice moved",)))

        result_cursor = FakeCursor(rows=[(3, "Song", "Artist")], description=["id", "title", "artist"])
        result = app.PostgresCursor(result_cursor)
        row = result.fetchone()
        self.assertEqual((row[0], row["title"], dict(row)), (3, "Song", {"id": 3, "title": "Song", "artist": "Artist"}))

    def test_existing_announcements_table_gets_edit_and_pin_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "existing.sqlite3"
            old = sqlite3.connect(database_path)
            old.execute("CREATE TABLE announcements (id INTEGER PRIMARY KEY, band_id INTEGER NOT NULL, member_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)")
            old.execute("INSERT INTO announcements(id,band_id,member_id,body,created_at) VALUES(1,2,3,'Old note','2026-10-17T12:00:00+00:00')")
            old.commit()
            old.close()

            with patch.object(app, "DB_PATH", database_path), patch.object(app, "BAND_DATABASE_URL", ""):
                app.init_db()
                conn = app.db_connect()
                try:
                    row = conn.execute("SELECT updated_at,is_pinned,version FROM announcements WHERE id=1").fetchone()
                    self.assertEqual(row["updated_at"], "2026-10-17T12:00:00+00:00")
                    self.assertEqual(row["is_pinned"], 0)
                    self.assertEqual(row["version"], 1)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
