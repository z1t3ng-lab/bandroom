import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
