import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

import app


class FakeHandler:
    """Capture JSON responses while calling the server route without a socket."""
    def __init__(self, cookie=None):
        self.cookie = cookie
        self.response = None
        self.status = None
        self.headers = None

    def send_json(self, data, status=200, headers=None):
        self.response, self.status, self.headers = data, status, headers

    def get_cookie(self, name):
        return self.cookie if name == "band_session" else None


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / "band.sqlite3"
        app.init_db()
        self.conn = app.db_connect()
        now = app.now_iso()
        self.band_id = self.conn.execute("INSERT INTO bands(name,timezone,created_at) VALUES(?,?,?)", ("Test Band", "America/Toronto", now)).lastrowid
        self.admin_id = self.add_member("Avery", "avery@example.test", "admin")
        self.member_id = self.add_member("Morgan", "member@example.test", "member")
        self.band = self.conn.execute("SELECT * FROM bands WHERE id=?", (self.band_id,)).fetchone()
        self.admin = self.member_row(self.admin_id)
        self.member = self.member_row(self.member_id)
        self.conn.commit()

    def tearDown(self):
        self.conn.close(); self.temp.cleanup()

    def add_member(self, name, email, role, band_id=None):
        now = app.now_iso()
        return self.conn.execute("""INSERT INTO members(band_id,display_name,email,password_hash,role,created_at)
            VALUES(?,?,?,?,?,?)""", (band_id or self.band_id, name, email, app.password_hash("test-password-2026"), role, now)).lastrowid

    def member_row(self, member_id):
        return self.conn.execute("""SELECT m.*,b.name AS band_name,b.timezone AS band_timezone FROM members m
            JOIN bands b ON b.id=m.band_id WHERE m.id=?""", (member_id,)).fetchone()

    def fake_handler(self):
        handler = object.__new__(app.Handler)
        FakeHandler.__init__(handler)
        handler.send_json = lambda data, status=200, headers=None: setattr(handler, "response", data) or setattr(handler, "status", status) or setattr(handler, "headers", headers)
        handler.get_cookie = lambda name: handler.cookie if name == "band_session" else None
        return handler

    def call(self, member, path, method="GET", query=None, body=None):
        handler = self.fake_handler()
        app.Handler.route(handler, self.conn, member, self.band if member["band_id"] == self.band_id else self.conn.execute("SELECT * FROM bands WHERE id=?", (member["band_id"],)).fetchone(), path, method, query or {}, body or {})
        return handler.status, handler.response

    def test_authentication_invitation_and_admin_permissions(self):
        with self.assertRaises(app.ApiError) as unauth:
            app.require_member(FakeHandler())
        self.assertEqual(unauth.exception.status, 401)
        status, invitation = self.call(self.admin, "/api/invitations", "POST")
        self.assertEqual(status, 201)
        token = invitation["invite_url"].split("token=", 1)[1]
        auth = self.fake_handler()
        app.Handler.auth_route(auth, None, "/api/join", {"token":token,"display_name":"New Member","email":"new@example.test","password":"new-member-password"})
        self.assertEqual(auth.status, 200)
        created = self.conn.execute("SELECT * FROM members WHERE email='new@example.test'").fetchone()
        self.assertIsNotNone(created)
        self.assertTrue(app.password_ok("new-member-password", created["password_hash"]))
        self.assertNotEqual(created["color"],self.admin["color"])
        session_cookie=SimpleCookie();session_cookie.load(auth.headers["Set-Cookie"])
        verified_conn,verified_member,_=app.require_member(FakeHandler(session_cookie["band_session"].value))
        self.assertEqual(verified_member["email"],"new@example.test")
        verified_conn.close()
        with self.assertRaises(app.ApiError) as forbidden:
            self.call(self.member, "/api/invitations", "POST")
        self.assertEqual(forbidden.exception.status, 403)

    def test_multiple_bands_have_separate_signup_and_band_scoped_login(self):
        status, song = self.call(self.admin, "/api/songs", "POST", body={
            "title": "First Band Song", "artist": "Aster Avenue"})
        self.assertEqual(status, 201)
        self.conn.commit()

        second_signup = self.fake_handler()
        app.Handler.auth_route(second_signup, None, "/api/signup", {
            "band_name": "Second Band", "display_name": "Taylor",
            "email": "avery@example.test", "password": "second-band-password"})
        self.assertEqual(second_signup.status, 200)
        self.conn.commit()
        second_band = self.conn.execute("SELECT * FROM bands WHERE name='Second Band'").fetchone()
        self.assertIsNotNone(second_band)
        second_admin_id = self.conn.execute(
            "SELECT id FROM members WHERE band_id=? AND email=?", (second_band["id"], "avery@example.test")
        ).fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bands").fetchone()[0], 2)

        login = self.fake_handler()
        app.Handler.auth_route(login, None, "/api/login", {
            "band_name": "Second Band", "email": "avery@example.test",
            "password": "second-band-password"})
        self.assertEqual(login.status, 200)
        cookie = SimpleCookie(); cookie.load(login.headers["Set-Cookie"])
        verified_conn, verified_member, verified_band = app.require_member(
            FakeHandler(cookie["band_session"].value))
        try:
            self.assertEqual(verified_member["id"], second_admin_id)
            self.assertEqual(verified_band["name"], "Second Band")
            status, response = self.call(verified_member, "/api/songs")
            self.assertEqual(status, 200)
            self.assertEqual(response["songs"], [])
            self.assertNotIn(song["title"], {item["title"] for item in response["songs"]})
        finally:
            verified_conn.close()

        original_login = self.fake_handler()
        app.Handler.auth_route(original_login, None, "/api/login", {
            "band_name": "Test Band", "email": "avery@example.test",
            "password": "test-password-2026"})
        self.assertEqual(original_login.status, 200)
        cookie = SimpleCookie(); cookie.load(original_login.headers["Set-Cookie"])
        original_conn, original_member, original_band = app.require_member(
            FakeHandler(cookie["band_session"].value))
        try:
            self.assertEqual(original_member["id"], self.admin_id)
            self.assertEqual(original_band["name"], "Test Band")
        finally:
            original_conn.close()

        wrong_band_login = self.fake_handler()
        with self.assertRaises(app.ApiError) as wrong_band:
            app.Handler.auth_route(wrong_band_login, None, "/api/login", {
                "band_name": "Test Band", "email": "avery@example.test",
                "password": "second-band-password"})
        self.assertEqual(wrong_band.exception.status, 401)

        duplicate_name = self.fake_handler()
        with self.assertRaises(app.ApiError) as duplicate:
            app.Handler.auth_route(duplicate_name, None, "/api/signup", {
                "band_name": "sEcOnD bAnD", "display_name": "Another Taylor",
                "email": "another@example.test", "password": "another-band-password"})
        self.assertEqual(duplicate.exception.status, 409)

    def test_password_reset_sends_one_time_link_changes_password_and_revokes_sessions(self):
        sent = []
        settings = {"public_url": "https://bandroom.example.test"}
        with patch.object(app, "password_reset_email_config", return_value=settings), \
                patch.object(app, "send_password_reset_email", side_effect=lambda *args: sent.append(args)):
            request = self.fake_handler()
            app.Handler.password_reset_route(request, "/api/password-reset/request", {
                "band_name": "Test Band", "email": "member@example.test"})
        self.assertEqual(request.status, 200)
        self.assertIn("If that account exists", request.response["message"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "member@example.test")
        reset_url = sent[0][2]
        self.assertIn("#reset=", reset_url)
        token = reset_url.split("#reset=", 1)[1]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        reset = self.conn.execute("SELECT * FROM password_resets WHERE token_hash=?", (token_hash,)).fetchone()
        self.assertIsNotNone(reset)
        self.assertNotIn(token, reset["token_hash"])
        self.assertGreater(app.datetime.fromisoformat(reset["expires_at"]), app.utc_now())

        session_token = "existing-session-token"
        self.conn.execute("INSERT INTO sessions(token_hash,member_id,expires_at,created_at) VALUES(?,?,?,?)",
                          (hashlib.sha256(session_token.encode()).hexdigest(), self.member_id,
                           (app.utc_now() + timedelta(days=3)).isoformat(timespec="seconds"), app.now_iso()))
        self.conn.commit()
        complete = self.fake_handler()
        app.Handler.password_reset_route(complete, "/api/password-reset/complete", {
            "token": token, "password": "brand-new-password-2026",
            "password_confirmation": "brand-new-password-2026"})
        self.assertEqual(complete.status, 200)
        member = self.conn.execute("SELECT password_hash FROM members WHERE id=?", (self.member_id,)).fetchone()
        self.assertTrue(app.password_ok("brand-new-password-2026", member["password_hash"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions WHERE member_id=?", (self.member_id,)).fetchone()[0], 0)
        used = self.conn.execute("SELECT used_at FROM password_resets WHERE id=?", (reset["id"],)).fetchone()
        self.assertIsNotNone(used["used_at"])
        with self.assertRaises(app.ApiError) as replay:
            app.Handler.password_reset_route(self.fake_handler(), "/api/password-reset/complete", {
                "token": token, "password": "another-new-password-2026",
                "password_confirmation": "another-new-password-2026"})
        self.assertEqual(replay.exception.status, 400)

    def test_password_reset_does_not_disclose_unknown_accounts_and_limits_email(self):
        sent = []
        settings = {"public_url": "https://bandroom.example.test"}
        with patch.object(app, "password_reset_email_config", return_value=settings), \
                patch.object(app, "send_password_reset_email", side_effect=lambda *args: sent.append(args)):
            known = self.fake_handler()
            app.Handler.password_reset_route(known, "/api/password-reset/request", {
                "band_name": "Test Band", "email": "member@example.test"})
            unknown = self.fake_handler()
            app.Handler.password_reset_route(unknown, "/api/password-reset/request", {
                "band_name": "Test Band", "email": "nobody@example.test"})
            self.assertEqual(known.response, unknown.response)
            self.assertEqual(len(sent), 1)
            for _ in range(2):
                app.Handler.password_reset_route(self.fake_handler(), "/api/password-reset/request", {
                    "band_name": "Test Band", "email": "member@example.test"})
            app.Handler.password_reset_route(self.fake_handler(), "/api/password-reset/request", {
                "band_name": "Test Band", "email": "member@example.test"})
        self.assertEqual(len(sent), 3)

    def test_password_reset_rejects_mismatched_and_expired_tokens(self):
        handler = self.fake_handler()
        with self.assertRaises(app.ApiError) as mismatch:
            app.Handler.password_reset_route(handler, "/api/password-reset/complete", {
                "token": "some-reset-token", "password": "brand-new-password-2026",
                "password_confirmation": "different-new-password-2026"})
        self.assertEqual(mismatch.exception.status, 400)

        token = "expired-reset-token"
        self.conn.execute("INSERT INTO password_resets(member_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
                          (self.member_id, hashlib.sha256(token.encode()).hexdigest(),
                           (app.utc_now() - timedelta(minutes=1)).isoformat(timespec="seconds"), app.now_iso()))
        self.conn.commit()
        with self.assertRaises(app.ApiError) as expired:
            app.Handler.password_reset_route(self.fake_handler(), "/api/password-reset/complete", {
                "token": token, "password": "brand-new-password-2026",
                "password_confirmation": "brand-new-password-2026"})
        self.assertEqual(expired.exception.status, 400)

    def test_private_band_scope_isolation(self):
        status, first = self.call(self.admin, "/api/songs", "POST", body={"title":"First Band Song","artist":"Aster Avenue","status":"planned"})
        self.assertEqual(status, 201)
        now = app.now_iso()
        other_band = self.conn.execute("INSERT INTO bands(name,timezone,created_at) VALUES(?,?,?)", ("Other Band","America/Toronto",now)).lastrowid
        other_member_id = self.add_member("Taylor","taylor@other.test","admin",other_band)
        other_member = self.member_row(other_member_id)
        self.conn.execute("""INSERT INTO songs(band_id,title,artist,status,priority,created_by,updated_by,created_at,updated_at)
            VALUES(?,?,?,'suggested',3,?,?,?,?)""", (other_band,"Other Band Only","Blue Atlas",other_member_id,other_member_id,now,now))
        status, response = self.call(other_member, "/api/songs")
        self.assertEqual(status, 200)
        titles={song["title"] for song in response["songs"]}
        self.assertEqual(titles,{"Other Band Only"})
        self.assertNotIn(first["title"],titles)

    def test_song_create_edit_and_stale_edit_conflict(self):
        status,song=self.call(self.admin,"/api/songs","POST",body={"title":"Glass Harbour","artist":"The Lumen Lines","status":"learning","priority":4})
        self.assertEqual(status,201,song)
        status,edited=self.call(self.member,f"/api/songs/{song['id']}","PATCH",body={"title":"Glass Harbour (Band Version)","version":song["version"]})
        self.assertEqual(status,200,edited)
        self.assertEqual(edited["updated_by_member"]["display_name"],"Morgan")
        with self.assertRaises(app.ApiError) as conflict:
            self.call(self.admin,f"/api/songs/{song['id']}","PATCH",body={"title":"Overwritten title","version":song["version"]})
        self.assertEqual(conflict.exception.status,409)
        status,latest=self.call(self.admin,f"/api/songs/{song['id']}")
        self.assertEqual(status,200)
        self.assertEqual(latest["title"],"Glass Harbour (Band Version)")

    def test_rehearsal_create_edit_and_member_attendance(self):
        day=(date.today()+timedelta(days=8)).isoformat()
        status,rehearsal=self.call(self.admin,"/api/rehearsals","POST",body={"title":"Tuesday practice","date":day,"start_time":"18:00","end_time":"20:00","status":"proposed","total_cost_cad":25.01})
        self.assertEqual(status,201,rehearsal)
        self.assertEqual(rehearsal["duration_minutes"],120)
        status,song=self.call(self.admin,"/api/songs","POST",body={"title":"Setlist test","artist":"Aster Avenue"})
        status,rehearsal=self.call(self.member,f"/api/rehearsals/{rehearsal['id']}/songs","POST",body={"song_id":song["id"],"version":rehearsal["version"]})
        self.assertEqual(status,200)
        with self.assertRaises(app.ApiError) as stale_setlist:
            self.call(self.admin,f"/api/rehearsals/{rehearsal['id']}/songs","POST",body={"song_id":song["id"],"version":1})
        self.assertEqual(stale_setlist.exception.status,409)
        status,updated=self.call(self.member,f"/api/rehearsals/{rehearsal['id']}","PATCH",body={"version":rehearsal["version"],"title":"Tuesday rehearsal","date":day,"start_time":"18:30","end_time":"20:30","status":"confirmed"})
        self.assertEqual(status,200,updated)
        self.assertEqual(updated["title"],"Tuesday rehearsal")
        status,response=self.call(self.member,f"/api/rehearsals/{rehearsal['id']}/response","POST",body={"response":"attending"})
        self.assertEqual(status,200,response)
        self.assertEqual(sum(item["amount_cents"] for item in response["cost_shares"]),2501)
        with self.assertRaises(app.ApiError) as conflict:
            self.call(self.admin,f"/api/rehearsals/{rehearsal['id']}","PATCH",body={"version":rehearsal["version"],"title":"Stale edit","date":day,"start_time":"18:00","end_time":"20:00","status":"proposed"})
        self.assertEqual(conflict.exception.status,409)

    def test_location_rate_snapshot_and_manual_cost_override(self):
        status, created = self.call(self.admin,"/api/locations","POST",body={"name":"Practice room","address":"100 Music Lane","hourly_rate":42.5})
        self.assertEqual(status,201)
        location = self.conn.execute("SELECT * FROM locations WHERE id=?",(created["id"],)).fetchone()
        day=(date.today()+timedelta(days=10)).isoformat()
        status,rehearsal=self.call(self.admin,"/api/rehearsals","POST",body={"title":"Rate test","event_type":"performance","date":day,"start_time":"18:00","end_time":"20:00","location_id":location["id"]})
        self.assertEqual(status,201)
        self.assertEqual(rehearsal["event_type"],"performance")
        self.assertEqual(rehearsal["total_cost_cad"],85.0)
        status,location_list=self.call(self.admin,"/api/locations")
        changed=location_list["locations"][0]
        self.call(self.admin,f"/api/locations/{changed['id']}","PATCH",body={"version":changed["version"],"name":"Practice room","address":"100 Music Lane","hourly_rate":60})
        status,updated=self.call(self.admin,f"/api/rehearsals/{rehearsal['id']}","PATCH",body={"version":rehearsal["version"],"title":"Rate test","event_type":"performance","date":day,"start_time":"18:00","end_time":"21:00","status":"proposed","location_id":location["id"]})
        self.assertEqual(updated["total_cost_cad"],127.5)
        status,manual=self.call(self.admin,f"/api/rehearsals/{rehearsal['id']}","PATCH",body={"version":updated["version"],"title":"Rate test","event_type":"performance","date":day,"start_time":"18:00","end_time":"21:30","status":"proposed","location_id":location["id"],"total_cost_cad":99.99})
        status,kept=self.call(self.admin,f"/api/rehearsals/{rehearsal['id']}","PATCH",body={"version":manual["version"],"title":"Rate test","event_type":"performance","date":day,"start_time":"18:00","end_time":"22:00","status":"proposed","location_id":location["id"]})
        self.assertEqual(kept["total_cost_cad"],99.99)


if __name__ == "__main__":
    unittest.main()
