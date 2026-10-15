#!/usr/bin/env python3
"""Create an isolated, fictional demo band database (never the default database)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", default=str(ROOT / "data" / "demo.sqlite3"), help="Path for a new demo database")
args = parser.parse_args()
output = Path(args.output).expanduser().resolve()
if output.exists():
    print(f"Refusing to overwrite existing database: {output}", file=sys.stderr)
    sys.exit(2)
os.environ["BAND_DB"] = str(output)

import app

app.init_db()
conn = app.db_connect()
zone = ZoneInfo("America/Toronto")
today = datetime.now(zone).date()
now = app.now_iso()
band_id = conn.execute("INSERT INTO bands(name,timezone,created_at) VALUES(?,?,?)", ("Northbound Signals", "America/Toronto", now)).lastrowid

members_data = [
    ("Riley Park", "riley@northbound.test", "admin", ["guitarist"], "#72a995"),
    ("Jordan Lee", "jordan@northbound.test", "member", ["drummer"], "#d28a68"),
    ("Sam Rivera", "sam@northbound.test", "member", ["bassist"], "#8b80b8"),
    ("Casey Chen", "casey@northbound.test", "member", ["other"], "#668cab"),
]
member_ids = {}
for display, email, role, roles, color in members_data:
    member_ids[email] = conn.execute("""INSERT INTO members(band_id,display_name,email,password_hash,role,roles_json,color,created_at)
      VALUES(?,?,?,?,?,?,?,?)""", (band_id, display, email, app.password_hash("BandroomDemo2026!"), role, json.dumps(roles), color, now)).lastrowid
riley, jordan, sam, casey = [member_ids[x[1]] for x in members_data]

location_id = conn.execute("""INSERT INTO locations(band_id,name,address,map_url,hourly_rate,contact,booking_link,equipment_json,notes,created_by,updated_at)
 VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (band_id, "Cedar Room Rehearsal Studio", "184 King Street West, Toronto, ON", "https://maps.google.com/?q=184+King+Street+West+Toronto", 42.50, "hello@cedarroom.test", "https://example.com/cedar-room-booking", json.dumps(["Drum kit", "Piano", "Guitar amps", "Bass amp", "Microphones"]), "Load in through the side entrance. Street parking after 6 pm.", riley, now)).lastrowid

song_rows = [
    {"title":"Golden Hourline","artist":"Mira Vale","status":"practicing","singers":[casey],"piano":riley,"key":"A major","bpm":108,"duration":4,"difficulty":3,"priority":5,"tags":["indie pop","energetic"],"arr":"Two-bar guitar intro; hold the final chorus before the ending.","practice":"Tighten the stop before the bridge. Goal: clean transition into the last chorus."},
    {"title":"Quiet Signal","artist":"The Amber State","status":"learning","singers":[riley,casey],"piano":casey,"key":"D minor","bpm":82,"duration":5,"difficulty":4,"priority":4,"tags":["slow","acoustic"],"arr":"Keep verse one sparse; add harmony on the last refrain.","practice":"Work on the bridge vocal blend and even out the soft verse dynamic."},
    {"title":"Summer Static","artist":"June Arcade","status":"planned","singers":[casey],"piano":None,"key":"E","bpm":126,"duration":3,"difficulty":2,"priority":3,"tags":["rock","energetic"],"arr":"Short guitar pickup; no solo on the first pass.","practice":"Lock the ending and check the tempo count-in."},
    {"title":"Paper Moons","artist":"Olive & North","status":"suggested","singers":[riley],"piano":casey,"key":"C","bpm":94,"duration":4,"difficulty":3,"priority":4,"tags":["folk","acoustic"],"arr":"Try a brushed verse groove, then open up for the chorus.","practice":"Listen to the reference and decide whether to keep the original key."},
    {"title":"After the Raincheck","artist":"The Lantern Club","status":"ready","singers":[casey],"piano":None,"key":"G","bpm":116,"duration":4,"difficulty":2,"priority":2,"tags":["indie rock"],"arr":"Guitar and vocal stop together on the final chord.","practice":"Ready for a set; keep the count-in clear."},
    {"title":"Little Northbound","artist":"Mira Vale","status":"archived","singers":[riley,casey],"piano":None,"key":"F","bpm":90,"duration":3,"difficulty":3,"priority":1,"tags":["slow"],"arr":"Archived while we revisit the arrangement.","practice":""},
]
song_ids=[]
for i,data in enumerate(song_rows):
    edited_by=[riley,jordan,sam,casey][i%4]
    sid=conn.execute("""INSERT INTO songs(band_id,title,artist,status,performed_live,performed_dates_json,singer_ids_json,pianist_id,key_name,bpm,duration_minutes,difficulty,priority,tags_json,links_json,arrangement_notes,practice_notes,created_by,updated_by,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(band_id,data["title"],data["artist"],data["status"],int(i==4),json.dumps([today.isoformat()] if i==4 else []),json.dumps(data["singers"]),data["piano"],data["key"],data["bpm"],data["duration"],data["difficulty"],data["priority"],json.dumps(data["tags"]),json.dumps([{"label":"Reference recording","url":"https://example.com/reference-track"}]),data["arr"],data["practice"],riley,edited_by,now,now)).lastrowid
    song_ids.append(sid)
    if i in (0,1,2,3):
        conn.execute("INSERT INTO song_votes(song_id,member_id,value,created_at) VALUES(?,?,1,?)",(sid,[riley,jordan,sam,casey][i],now))
conn.execute("INSERT INTO song_comments(song_id,member_id,body,created_at) VALUES(?,?,?,?)",(song_ids[0],jordan,"The bridge feels good at this tempo. Let’s work on the stop together next time.",now))
conn.execute("INSERT INTO song_comments(song_id,member_id,body,created_at) VALUES(?,?,?,?)",(song_ids[1],casey,"I can take the harmony on the final refrain.",now))

# One rehearsal is proposed, with two early RSVPs to demonstrate cost shares.
days_to_sunday = (6-today.weekday()) % 7 or 7
rehearsal_day = today + timedelta(days=days_to_sunday)
start_local = datetime.combine(rehearsal_day, time(18,30), zone)
end_local = datetime.combine(rehearsal_day, time(20,30), zone)
rid = conn.execute("""INSERT INTO rehearsals(band_id,title,start_utc,end_utc,timezone,status,organizer_id,invited_ids_json,location_id,location_name_snapshot,address_snapshot,room_number,booking_link,booking_reference,total_cost_cad,split_mode,split_member_ids_json,cost_override,hourly_rate_snapshot,flat_cost_snapshot,goals,notes,created_at,updated_at,updated_by)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(band_id,"Sunday set-building",start_local.astimezone(timezone.utc).isoformat(),end_local.astimezone(timezone.utc).isoformat(),"America/Toronto","proposed",riley,json.dumps([riley,jordan,sam,casey]),location_id,"Cedar Room Rehearsal Studio","184 King Street West, Toronto, ON","Studio B","https://example.com/cedar-room-booking","CR-204",85.00,"attending","[]",0,42.50,None,"Tighten the bridge in Golden Hourline and run the new set in order.","Bring the current charts; the studio has a piano and full backline.",now,now,riley)).lastrowid
for mid,response in [(riley,"attending"),(jordan,"attending"),(sam,"maybe"),(casey,"no_response")]:
    conn.execute("INSERT INTO rehearsal_attendance(rehearsal_id,member_id,response) VALUES(?,?,?)",(rid,mid,response))
for position,song_id,note in [(1,song_ids[0],"Focus on the bridge stop"),(2,song_ids[1],"Try the harmony in the final refrain"),(3,song_ids[2],"Check the count-in")]:
    conn.execute("INSERT INTO rehearsal_songs(rehearsal_id,song_id,position,notes) VALUES(?,?,?,?)",(rid,song_id,position,note))
app.recalculate_shares(conn,rid)

# Recurring Sunday windows, plus one-off periods and explicit unavailable time.
next_sunday = rehearsal_day + timedelta(days=7)
availability_data=[
    (riley,"available",next_sunday,"17:00","21:00","weekly",next_sunday+timedelta(days=35),"Usually free after dinner"),
    (jordan,"available",next_sunday,"18:00","22:00","weekly",next_sunday+timedelta(days=35),"Can bring the practice pad"),
    (sam,"available",next_sunday,"16:30","19:30","none",None,"Free until 7:30"),
    (casey,"unavailable",next_sunday,None,None,"none",None,"Out of town that Sunday"),
    (casey,"available",next_sunday+timedelta(days=7),"18:00","21:00","none",None,"Available next week"),
]
for mid,kind,day,st,et,recurrence,until,note in availability_data:
    conn.execute("""INSERT INTO availability(band_id,member_id,kind,start_date,start_time,end_time,recurrence,until_date,note,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)""",(band_id,mid,kind,day.isoformat(),st,et,recurrence,until.isoformat() if until else None,note,now))

for mid,action,entity_type,entity_id,title in [(riley,"added a song","song",song_ids[0],"Golden Hourline"),(jordan,"updated a song","song",song_ids[1],"Quiet Signal"),(riley,"planned a rehearsal","rehearsal",rid,"Sunday set-building")]:
    conn.execute("INSERT INTO activity(band_id,member_id,action,entity_type,entity_id,entity_title,created_at) VALUES(?,?,?,?,?,?,?)",(band_id,mid,action,entity_type,entity_id,title,now))
conn.commit();conn.close()
print(f"Created isolated demo database: {output}")
print("Demo logins (same demo-only password for all accounts):")
for _,email,_,_,_ in members_data:
    print(f"  {email} / BandroomDemo2026!")
print("Start it with: BAND_DB=data/demo.sqlite3 python3 app.py")
