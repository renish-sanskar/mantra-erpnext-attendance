import json
import os
import sqlite3
import tempfile
from datetime import datetime

import app


def create_mock(path):
    """A small Mantra-like SQLite table: 10 employees, IN at 09:00 and OUT at 18:00 on 12 days. Returns the row count."""
    rows = [(str(1000 + i), f"Emp {i}", f"2026-11-{d:02d} {h:02d}:00:00", m, "Gate-1")
            for i in range(1, 11) for d in range(1, 13) for h, m in ((9, "I"), (18, "O"))]
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE tmpdmpTerminalData (EmployeeCode TEXT, EmployeeName TEXT, PunchDateTime DATETIME, InOutMode TEXT, TerminalName TEXT)")
        con.executemany("INSERT INTO tmpdmpTerminalData VALUES (?,?,?,?,?)", rows)
    return len(rows)


tmp = tempfile.mkdtemp()
app.DB = os.path.join(tmp, "t.db")
app.init()

# --- standard payload
s = app.standardize({"emp_code": 1001.0, "punch_time": "2026-11-25 09:00:00", "log_type": "in", "device": "Gate-1", "name": "Asha"})
assert (s["tx_id"], s["emp_code"], s["log_type"], s["device"], s["name"]) == ("1001|2026-11-25 09:00:00", "1001", "IN", "Gate-1", "Asha"), s
assert app.standardize({"EMP_CODE": "7", "PUNCH_DATETIME": "04-03-2024 09:00:00", "PUNCH_STATE": "Check Out", "TERMINAL_ALIAS": "G", "FIRST_NAME": "A", "LAST_NAME": "B"})["log_type"] == "OUT"
assert app.standardize({"emp_code": "7", "punch_time": datetime(2026, 11, 25, 9, 0, 0, 999)})["punch_time"] == "2026-11-25 09:00:00"
assert app.standardize({"emp_code": "7", "punch_time": "25/11/2026 09:00:00"})["punch_time"] == "2026-11-25 09:00:00"
assert app.standardize({"emp_code": "7", "punch_time": "2026-11-25 09:00:00"})["log_type"] is None
for bad in ({"punch_time": "2026-11-25 09:00:00"}, {"emp_code": "7"}, {"emp_code": "7", "punch_time": "soon"}, {"EMP_CODE": "7", "PUNCH_TIME": "09:00:00"}):
    try:
        app.standardize(bad)
        raise SystemExit(f"should have failed: {bad}")
    except (KeyError, ValueError, TypeError):
        pass
assert [app.parse_state(v) for v in ("I", "O", 0, 1, "Check In", "Break Out", "?", None)] == ["IN", "OUT", "IN", "OUT", "IN", "OUT", None, None]

# --- SQL Server connection string (the real driver can't run here)
c = {"m_server": r".\Mantra2022", "m_db": "PayTime", "m_user": "sa", "m_pass": "p;w}d"}
cs = app.conn_str(c, ["SQL Server", "ODBC Driver 17 for SQL Server"])
assert cs == r"DRIVER={ODBC Driver 17 for SQL Server};SERVER=.\Mantra2022;DATABASE=PayTime;UID={sa};PWD={p;w}}d};TrustServerCertificate=yes;", cs
assert app.conn_str(c, ["SQL Server"]).startswith("DRIVER={SQL Server};") and "Trust" not in app.conn_str(c, ["SQL Server"])
try:
    app.conn_str(c, ["PostgreSQL"])
    raise SystemExit("should have failed")
except RuntimeError:
    pass

# --- mock Mantra table
mock = os.path.join(tmp, "PayTime.db")
total = create_mock(mock)
assert total > 100
cfg = {"m_server": "sqlite:" + mock, "m_table": "tmpdmpTerminalData"}
app.set_cfg(**cfg, start_date="2000-01-01")
m = app.Mantra(app.cfg())
assert [n for n, _ in m.columns()] == ["EmployeeCode", "EmployeeName", "PunchDateTime", "InOutMode", "TerminalName"]
assert m.count() == total and len(m.sample()) == 5
for evil in ("x; DROP TABLE y", 'a"b', "[a]", "", "a.b;c"):
    try:
        m.q(evil)
        raise SystemExit(f"unsafe name accepted: {evil!r}")
    except ValueError:
        pass
assert m.q("dbo.tmpdmpTerminalData") == '"dbo"."tmpdmpTerminalData"'
m.close()
assert app.guess(app.Mantra(app.cfg()).columns()) == {"m_emp": "EmployeeCode", "m_time": "PunchDateTime", "m_state": "InOutMode", "m_device": "TerminalName", "m_name": "EmployeeName"}

assert app.guess([("In_Out_Time", "datetime"), ("Mode", "varchar"), ("CardNo", "varchar"), ("Attn_Dt", "datetime")]) == \
    {"m_emp": "CardNo", "m_time": "In_Out_Time", "m_state": "Mode"}  # the real Mantra tmpDmpTerminalData columns
real = [("EmpCode", "int"), ("In_Out_Time", "datetime"), ("ShiftCode", "int"), ("Attn_Dt", "datetime"), ("Mode", "varchar"),
        ("CreatedDate", "datetime"), ("RecDet", "int"), ("IPaddress", "varchar"), ("DeviceCode", "int"), ("Extra1", "varchar"),
        ("EntryMode", "int"), ("AttnDt", "int"), ("InOutTime", "bigint"), ("CardNO", "int"), ("EmpID", "int"), ("Sync", "int"),
        ("Location", "varchar"), ("Latitude", "varchar"), ("Longitude", "varchar"), ("MReason", "varchar"), ("LogImage", "image")]
assert app.guess(real) == {"m_emp": "CardNO", "m_time": "In_Out_Time", "m_state": "Mode", "m_device": "DeviceCode",
                           "m_lat": "Latitude", "m_lng": "Longitude"}  # EmpCode/EmpID are always 0; InOutTime is a number, not In/Out
# the database reader keeps a punch whose coordinates are junk; the API rejects it
junk = {"emp_code": "2", "punch_time": "2020-08-20 16:32:00", "latitude": "None", "longitude": "x"}
assert app.standardize(junk)["latitude"] is None and app.standardize(junk)["longitude"] is None
try:
    app.standardize(junk, strict=True)
    raise SystemExit("API should reject junk coordinates")
except ValueError:
    pass

# --- employee lookup: Active only, one request per code, errors surface as ERPDown
real_http, seen = app.http, []


class FakeHttp:
    def __init__(self, status=200, data=()):
        self.status, self.data = status, list(data)

    def get(self, url, **kw):
        seen.append(json.loads(kw["params"]["filters"]))
        return type("R", (), {"ok": self.status == 200, "status_code": self.status, "text": "boom", "json": lambda s: {"data": self.data}})()


c0 = {"erp_url": "http://x", "erp_key": "k", "erp_secret": "s", "employee_field": "attendance_device_id"}
app.http = FakeHttp(data=[{"name": "HR-EMP-1"}])
assert app.active_employee(c0, "55") == "HR-EMP-1" and app.active_employee(c0, "55") == "HR-EMP-1" and len(seen) == 1
assert seen[0] == [["attendance_device_id", "=", "55"], ["status", "=", "Active"]]
app.http = FakeHttp(data=[])
assert app.active_employee(c0, "66") is None
assert app.push(c0, {"emp_code": "66"})[:2] == ("error", "No active Employee with attendance_device_id = 66")
app.http = FakeHttp(status=403)
try:
    app.active_employee(c0, "77")
    raise SystemExit("should have raised")
except app.ERPDown:
    pass
app.http = real_http

cl = app.app.test_client()
app.sync = app.sync  # real sync below, only ERPNext is faked
calls = []


def fake_push(c, r):
    calls.append(r["tx_id"])
    return "ok", "", "HR-EMP-" + r["emp_code"], "Emp " + r["emp_code"], "CHKIN-" + str(len(calls))


app.push = fake_push

assert "Choose the employee" in app.sync("manual")  # columns not mapped yet
r = cl.post("/mantra/detect", data={"m_server": "sqlite:" + mock, "m_table": "tmpdmpTerminalData"}, follow_redirects=True)
assert b"Connected. The table has" in r.data and b"EmployeeCode" in r.data and b"First rows of the table" in r.data
c = app.cfg()
assert (c["m_emp"], c["m_time"], c["m_state"]) == ("EmployeeCode", "PunchDateTime", "InOutMode")

print(app.sync("manual"))
with app.db() as d:
    n = d.execute("SELECT COUNT(*) FROM log").fetchone()[0]
    types = dict(d.execute("SELECT log_type, COUNT(*) FROM log GROUP BY log_type").fetchall())
    assert d.execute("SELECT COUNT(*) FROM log WHERE status='ok'").fetchone()[0] == n
assert n == total and set(types) == {"IN", "OUT"}, (n, total, types)  # I/O column mapped to IN/OUT
assert app.cfg()["last_ok_sync"] != ""
assert "Fetched 0 new" in app.sync("auto")  # the same rows again: nothing new, nothing pushed twice
assert len(calls) == total

# --- look-back: a row that lands in the table late still carries its old punch time
import sqlite3
from datetime import timedelta


def add_late(code):
    with sqlite3.connect(mock) as con:
        con.execute("INSERT INTO tmpdmpTerminalData VALUES (?,?,?,?,?)",
                    (code, "Late", (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"), "I", "Gate-1"))


def stored(code):
    with app.db() as d:
        return d.execute("SELECT COUNT(*) FROM log WHERE emp_code=?", (code,)).fetchone()[0]


app.set_cfg(lookback_days="0")
add_late("9001")
app.sync("auto")
assert stored("9001") == 0  # missed without the look-back
app.set_cfg(lookback_days="1")
app.sync("auto")
assert stored("9001") == 1 and sum(t.startswith("9001|") for t in calls) == 1  # caught with it
app.sync("auto")
assert sum(t.startswith("9001|") for t in calls) == 1  # reading again pushes nothing twice
app.set_cfg(lookback_days="oops")
add_late("9002")
app.sync("auto")
assert stored("9002") == 1  # an unreadable value falls back to 1 day
cl.post("/config", data={"lookback_days": "3"})
assert app.cfg()["lookback_days"] == "3" and b"Re-read the last (days)" in cl.get("/config").data

# --- no In/Out column: punches alternate IN, OUT per person per day
app.DB = os.path.join(tmp, "t2.db")
app.init()
app.set_cfg(**cfg, start_date="2000-01-01", m_emp="EmployeeCode", m_time="PunchDateTime", m_device="TerminalName")
app.sync("manual")
with app.db() as d:
    seq = [r[0] for r in d.execute("SELECT log_type FROM log WHERE emp_code='1001' AND substr(punch_time,1,10)=(SELECT MIN(substr(punch_time,1,10)) FROM log WHERE emp_code='1001') ORDER BY punch_time")]
assert seq == ["IN", "OUT"], seq

# --- webhook: standard payload and Easy TimePro style
tok = app.cfg()["webhook_token"]
body = [{"emp_code": "2001", "punch_time": "2026-11-26 09:00:00"}, {"emp_code": "2001", "punch_time": "2026-11-26 18:00:00"},
        {"EMP_CODE": "2002", "PUNCH_DATETIME": "26-11-2026 09:05:00", "PUNCH_STATE": "in"}, {"junk": 1}]
assert cl.post("/api/punches", json=body).status_code == 401
r = cl.post("/api/punches?token=" + tok, json=body)
assert r.status_code == 200 and r.json == {"received": 4, "new": 3, "unreadable": 1}, r.json
# the API takes only IN/OUT for log_type; machine codes and out-of-range coordinates are unreadable
bad = [{"emp_code": "2003", "punch_time": "2026-11-26 09:10:00", "log_type": v} for v in ("I", "0", "Check In", "BREAK")] + \
      [{"emp_code": "2003", "punch_time": "2026-11-26 09:11:00", "latitude": 91, "longitude": 10},
       {"emp_code": "2003", "punch_time": "2026-11-26 09:12:00", "latitude": "north", "longitude": 10}]
assert cl.post("/api/punches?token=" + tok, json=bad).json == {"received": 6, "new": 0, "unreadable": 6}
good = {"emp_code": "2003", "punch_time": "2026-11-26 09:13:00", "log_type": "out", "Lat": "23.0225", "LNG": 72.5714}
assert cl.post("/api/punches?token=" + tok, json=[good]).json["new"] == 1
with app.db() as d:
    assert tuple(d.execute("SELECT log_type, latitude, longitude FROM log WHERE emp_code='2003'").fetchone()) == ("OUT", 23.0225, 72.5714)
assert app.standardize({"emp_code": "7", "punch_time": "2026-11-26 09:00:00"})["latitude"] is None
assert app.standardize({"emp_code": "7", "punch_time": "2026-11-26 09:00:00", "log_type": "I"})["log_type"] == "IN"  # the database reader still understands I/O
assert cl.post("/api/punches", headers={"X-Token": tok}, json={"data": body[:1]}).json["new"] == 0  # already stored
assert cl.post("/api/punches?token=" + tok, json="x").status_code == 400
with app.db() as d:
    assert [r[0] for r in d.execute("SELECT log_type FROM log WHERE emp_code='2001' ORDER BY punch_time")] == ["IN", "OUT"]

# --- ERPNext down: stops after 3, rest stay queued
import requests
app.DB = os.path.join(tmp, "t3.db")
app.init()
app.ingest([{"emp_code": str(i), "punch_time": "2026-11-28 09:00:00", "log_type": "IN"} for i in range(5)], app.now())
calls.clear()


def down(c, r):
    calls.append(1)
    raise requests.ConnectionError("refused")


app.push = down
msg = app.sync(fetch=False)
assert len(calls) == 3 and "2 punches left queued" in msg and app.cfg()["last_ok_sync"] == "", (calls, msg)

# --- a machine that marks every punch as In: that column is not used, punches alternate IN, OUT
mock_in = os.path.join(tmp, "AllIn.db")
create_mock(mock_in)
with sqlite3.connect(mock_in) as con:
    con.execute("UPDATE tmpdmpTerminalData SET InOutMode='I'")
app.DB = os.path.join(tmp, "t4.db")
app.init()
app.push = fake_push
r = cl.post("/mantra/detect", data={"m_server": "sqlite:" + mock_in, "m_table": "tmpdmpTerminalData"}, follow_redirects=True)
assert b"only one value" in r.data and b"not used for In / Out" in r.data
c = app.cfg()
assert c["m_state"] == "" and (c["m_emp"], c["m_time"]) == ("EmployeeCode", "PunchDateTime")
assert json.loads(c["m_values"])["InOutMode"] == ["I"]
app.set_cfg(start_date="2000-01-01")
app.sync("manual")
with app.db() as d:
    seq = [r[0] for r in d.execute("SELECT log_type FROM log WHERE emp_code='1001' AND substr(punch_time,1,10)=(SELECT MIN(substr(punch_time,1,10)) FROM log WHERE emp_code='1001') ORDER BY punch_time")]
    types = {r[0] for r in d.execute("SELECT log_type FROM log")}
assert seq == ["IN", "OUT"] and types == {"IN", "OUT"}, (seq, types)
app.set_cfg(m_state="InOutMode")  # mapped anyway: the page warns
assert b"has only one value (I)" in cl.get("/config").data
assert b"InOutMode (TEXT) \xe2\x80\x93 I" in cl.get("/config").data  # the dropdown shows the values
app.set_cfg(m_state="")

# --- pages, paging, settings
with app.db() as d:
    d.executemany("INSERT INTO sync_run(started, trigger) VALUES(?, 'auto')", [("2026-11-01 10:00:00",)] * 120)
for u in ("/", "/logs", "/runs", "/config", "/runs?page=3", "/runs?page=abc", "/logs?status=ok&q=Emp"):
    h = cl.get(u).data
    assert cl.get(u).status_code == 200 and b"Attendance Sync" in h and b"{%" not in h and b"{{" not in h, u
assert b"Page 3 of 3" in cl.get("/runs?page=3").data
assert b"Mantra database" in cl.get("/config").data and b"standard payload" in cl.get("/config").data
app.set_cfg(m_pass="secret-pw")
cl.post("/config", data={"m_server": "x", "m_pass": ""})
assert app.cfg()["m_pass"] == "secret-pw" and app.cfg()["m_server"] == "x"  # blank password keeps the saved one
assert b"secret-pw" not in cl.get("/config").data

# --- latitude / longitude columns of the Mantra table
import sqlite3
assert app.guess([("CardNo", "varchar"), ("In_Out_Time", "datetime"), ("Latitude", "float"), ("Longitude", "float")]) == \
    {"m_emp": "CardNo", "m_time": "In_Out_Time", "m_lat": "Latitude", "m_lng": "Longitude"}
geo = os.path.join(tmp, "geo.db")
g = sqlite3.connect(geo)
g.execute("CREATE TABLE t(Code, T, Lat, Lng)")
g.executemany("INSERT INTO t VALUES(?,?,?,?)", [("5", "2026-11-26 09:00:00", 23.5, 72.5), ("6", "2026-11-26 09:01:00", None, None)])
g.commit()
g.close()
app.DB = os.path.join(tmp, "geo_cfg.db")
app.init()
app.set_cfg(m_server="sqlite:" + geo, m_table="t", m_emp="Code", m_time="T", m_lat="Lat", m_lng="Lng")
rows = list(app.fetch_punches(app.cfg(), datetime(2026, 1, 1)))
assert [(r["latitude"], r["longitude"]) for r in rows] == [(23.5, 72.5), (None, None)], rows
assert app.ingest(rows, app.now()) == (2, 0)
with app.db() as d:
    assert [tuple(r) for r in d.execute("SELECT latitude, longitude FROM log ORDER BY emp_code")] == [(23.5, 72.5), (None, None)]
assert b"Latitude" in cl.get("/config").data
print("OK")
