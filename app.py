from contextlib import closing, contextmanager
import csv, hmac, io, json, os, re, secrets, sqlite3, threading, time
from datetime import date, datetime, timedelta
from decimal import Decimal
import requests
from flask import Flask, Response, flash, jsonify, redirect, render_template_string, request, url_for

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")  # not cwd: boot tasks start in System32
PER_PAGE = 50
LABEL = {"ok": "Pushed", "duplicate": "Already in ERPNext", "error": "Failed", "pending": "Waiting", "run": "Running"}
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # only signs flash messages; a restart just clears them
lock = threading.Lock()
http = requests.Session()  # reuses connections across the many small ERPNext calls

DEFAULTS = {
    "m_server": "", "m_db": "", "m_user": "", "m_pass": "", "m_table": "",
    "m_emp": "", "m_time": "", "m_state": "", "m_device": "", "m_name": "", "m_lat": "", "m_lng": "",  # which table column holds what
    "m_cols": "[]", "m_sample": "[]", "m_values": "{}",  # filled in by "Save and read columns"
    "erp_url": "http://localhost:8000", "erp_key": "", "erp_secret": "",
    "employee_field": "attendance_device_id",  # Employee field holding the punch code
    "start_date": datetime.now().strftime("%Y-%m-%d"),  # first sync reads from here
    "interval_min": "5", "auto_sync": "0", "alt_inout": "0", "min_gap": "0", "last_sync": "", "lookback_days": "1",
    "webhook_token": secrets.token_urlsafe(16), "last_ok_sync": "",
}
# columns added after v1: (name, ddl) -> applied to an existing db by init()
LOG_COLS = {"employee": "TEXT", "employee_name": "TEXT", "checkin": "TEXT",
            "attempts": "INTEGER DEFAULT 0", "run_id": "INTEGER", "created": "TEXT",
            "latitude": "REAL", "longitude": "REAL"}


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        c.execute("CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS log(
            tx_id TEXT PRIMARY KEY, emp_code TEXT, punch_time TEXT, log_type TEXT,
            device TEXT, status TEXT, message TEXT, updated TEXT)""")
        have = {r["name"] for r in c.execute("PRAGMA table_info(log)")}
        for col, ddl in LOG_COLS.items():
            if col not in have:
                c.execute(f"ALTER TABLE log ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_run(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, trigger TEXT,
            fetched INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, duplicate INTEGER DEFAULT 0,
            error INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
        c.execute("CREATE INDEX IF NOT EXISTS log_time ON log(punch_time)")
        c.execute("DROP INDEX IF EXISTS log_status")
        c.execute("CREATE INDEX IF NOT EXISTS log_status_upd ON log(status, updated)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (k, v))


def cfg():
    with db() as c:
        return {r["k"]: r["v"] for r in c.execute("SELECT * FROM config")}


def set_cfg(**kw):
    with db() as c:
        for k, v in kw.items():
            c.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (k, v))


FIELDS = ("m_emp", "m_time", "m_state", "m_device", "m_name", "m_lat", "m_lng")
GUESS = {"m_emp": ("cardno", "empcode", "employeecode", "empid", "userid", "enroll", "emp", "user", "pin"),  # Mantra: EmpCode and EmpID are always 0, CardNO is the real code
         "m_time": ("inouttime", "punchdatetime", "datetime", "logtime", "punchtime", "punch", "date"),
         "m_state": ("inout", "state", "status", "mode", "direction", "type"),
         "m_device": ("terminal", "device", "machine", "reader"),
         "m_name": ("employeename", "empname", "username", "name"),
         "m_lat": ("latitude", "lat"), "m_lng": ("longitude", "lng", "long")}
DATE_TYPES = ("datetime", "datetime2", "smalldatetime", "date")
DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 13 for SQL Server",
           "SQL Server Native Client 11.0", "SQL Server")  # newest first; the last one ships with Windows


def conn_str(c, installed):
    drv = next((d for d in DRIVERS if d in installed), None)
    if not drv:
        raise RuntimeError("No SQL Server ODBC driver found. Install 'ODBC Driver 17 for SQL Server' from Microsoft.")
    esc = lambda v: "{" + v.replace("}", "}}") + "}"
    s = f"DRIVER={{{drv}}};SERVER={c['m_server'].strip()};DATABASE={c['m_db'].strip()};UID={esc(c['m_user'])};PWD={esc(c['m_pass'])};"
    return s + ("TrustServerCertificate=yes;" if drv.startswith("ODBC Driver") else "")


class Mantra:
    """Read-only connection to the Mantra database. A server value of "sqlite:<file>" is for testing without SQL Server."""

    def __init__(self, c):
        server = c["m_server"].strip()
        if server.lower().startswith("sqlite:"):
            self.kind, self.con = "sqlite", sqlite3.connect(server[7:])
        else:
            import pyodbc
            self.kind, self.con = "mssql", pyodbc.connect(conn_str(c, pyodbc.drivers()), timeout=15)
        self.table = c["m_table"].strip()

    def close(self):
        self.con.close()

    def q(self, name):
        parts = name.strip().split(".")
        if not all(re.fullmatch(r"\w[\w ]*", p) for p in parts):
            raise ValueError(f"unsafe or empty name {name!r}")
        w = "[{}]" if self.kind == "mssql" else '"{}"'
        return ".".join(w.format(p) for p in parts)

    def run(self, sql, args=()):
        cur = self.con.cursor()
        cur.execute(sql, args)
        return cur

    def columns(self):
        if self.kind == "mssql":
            cur = self.run("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=? ORDER BY ORDINAL_POSITION",
                           (self.table.split(".")[-1],))
            return [(r[0], r[1]) for r in cur.fetchall()]
        return [(r[1], r[2]) for r in self.run(f"PRAGMA table_info({self.q(self.table)})").fetchall()]

    def count(self):
        return self.run(f"SELECT COUNT(*) FROM {self.q(self.table)}").fetchone()[0]

    def sample(self, n=5):
        cur = self.run(f"SELECT TOP {n} * FROM {self.q(self.table)}" if self.kind == "mssql" else f"SELECT * FROM {self.q(self.table)} LIMIT {n}")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def distinct(self, col, n=6):
        top, lim = (f"TOP {n} ", "") if self.kind == "mssql" else ("", f" LIMIT {n}")
        cur = self.run(f"SELECT DISTINCT {top}{self.q(col)} FROM {self.q(self.table)}{lim}")
        return [("(empty)" if r[0] is None else str(r[0]).strip()) for r in cur.fetchall()]

    def rows(self, c, since):
        want = list(dict.fromkeys(c[k] for k in FIELDS if c[k]))
        tc = self.q(c["m_time"])
        sql, args = f"SELECT {', '.join(self.q(n) for n in want)} FROM {self.q(self.table)}", []
        if dict(json.loads(c["m_cols"])).get(c["m_time"], "").lower() in DATE_TYPES:  # real date column: let the server filter
            sql += f" WHERE {tc} >= ?"
            args = [since if self.kind == "mssql" else since.strftime("%Y-%m-%d %H:%M:%S")]
        cur = self.run(sql + f" ORDER BY {tc}", args)
        names = [d[0] for d in cur.description]
        while chunk := cur.fetchmany(1000):
            for r in chunk:
                yield dict(zip(names, r))


def guess(cols):
    """Pick a likely column for each field from the column names."""
    out, used = {}, set()
    for key, words in GUESS.items():
        for w in words:
            for n, _ in cols:
                low = n.lower().replace("_", "").replace(" ", "")
                if (key not in out and n not in used and w in low and (key != "m_emp" or "name" not in low)
                        and (key != "m_state" or not ("time" in low or "date" in low))):  # InOutTime is a time, not In/Out
                    out[key] = n
                    used.add(n)
    return out


def fetch_punches(c, since):
    """Rows of the Mantra table as standard punches (see README)."""
    if not (c["m_emp"] and c["m_time"]):
        raise ValueError("Choose the employee code and punch time columns in Settings first")
    with closing(Mantra(c)) as m:
        state = c["m_state"] if c["alt_inout"] != "1" else ""  # checkbox in Settings: ignore the column, alternate IN, OUT
        if state:  # a column that never says both IN and OUT (all "I", or an unknown OUT code) is useless: alternate instead
            try:
                if len({parse_state(v) for v in m.distinct(state, 50)} - {None}) < 2:
                    state = ""
            except Exception:
                pass  # column can't be listed: trust it
        for r in m.rows(c, since):
            try:
                if parse_time(r[c["m_time"]]) < since:
                    continue
            except ValueError:
                pass  # unreadable time: let ingest count it
            yield {"emp_code": r[c["m_emp"]], "punch_time": r[c["m_time"]],
                   "log_type": r[state] if state else None,
                   "device": r[c["m_device"]] if c["m_device"] else "", "name": r[c["m_name"]] if c["m_name"] else "",
                   "latitude": r[c["m_lat"]] if c["m_lat"] else None, "longitude": r[c["m_lng"]] if c["m_lng"] else None}


class ERPDown(Exception):
    """ERPNext unreachable / rejecting our credentials: not a problem with this one punch."""


def active_employee(c, code):
    """Employee id for a punch code, Active employees only (HRMS alone would also match someone who left). Cached per sync."""
    cache = c.setdefault("_emp", {})
    if code not in cache:
        r = http.get(c["erp_url"].rstrip("/") + "/api/resource/Employee",
                     headers={"Authorization": f"token {c['erp_key']}:{c['erp_secret']}"},
                     params={"filters": json.dumps([[c["employee_field"], "=", code], ["status", "=", "Active"]]),
                             "fields": '["name"]', "limit_page_length": 1}, timeout=30)
        if not r.ok:
            raise ERPDown(f"Employee lookup HTTP {r.status_code}: {r.text[:300]}")
        data = r.json().get("data") or []
        cache[code] = data[0]["name"] if data else None
    return cache[code]


def push(c, row):
    """-> (status, message, employee, employee_name, checkin_name)"""
    emp = active_employee(c, row["emp_code"])
    if not emp:
        return "error", f"No active Employee with {c['employee_field']} = {row['emp_code']}", None, None, None
    doc = {"employee": emp, "time": row["punch_time"], "log_type": row["log_type"], "device_id": row["device"]}
    for k in ("latitude", "longitude"):  # ERPNext derives geolocation and checks the shift-location radius from these
        if row[k] is not None:
            doc[k] = row[k]
    r = http.post(c["erp_url"].rstrip("/") + "/api/resource/Employee Checkin",  # same call as the old sync script: no custom method involved
                  headers={"Authorization": f"token {c['erp_key']}:{c['erp_secret']}"}, json=doc, timeout=30)
    if r.ok:
        d = r.json().get("data") or {}
        return "ok", "", d.get("employee"), d.get("employee_name"), d.get("name")
    msg = r.text[:300]
    if "already has a log" in msg:
        return "duplicate", "already in ERPNext", None, None, None
    if r.status_code in (401, 403) or r.status_code >= 500:
        raise ERPDown(f"HTTP {r.status_code}: {msg}")
    return "error", msg, None, None, None


def push_row(c, row, run_id):
    """-> (status, erpnext_down)"""
    down = False
    try:
        res = push(c, row)  # network happens outside any DB transaction
    except (requests.RequestException, ERPDown) as e:
        res, down = ("error", str(e)[:300], None, None, None), True
    with db() as d:
        d.execute("""UPDATE log SET status=?, message=?, employee=COALESCE(?,employee),
                     employee_name=COALESCE(?,employee_name), checkin=COALESCE(?,checkin),
                     attempts=attempts+1, run_id=?, updated=? WHERE tx_id=?""",
                  (res[0], res[1], res[2], res[3], res[4], run_id, now(), row["tx_id"]))
    return res[0], down


TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M")


def txt(v):
    if isinstance(v, (float, Decimal)) and v == int(v):
        v = int(v)
    return "" if v is None else str(v).strip()


def parse_time(v):
    if isinstance(v, datetime):
        return v.replace(microsecond=0, tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    raw = txt(v)[:19]
    for f in TIME_FORMATS:
        try:
            return datetime.strptime(raw, f)
        except ValueError:
            pass
    raise ValueError(f"bad punch time {v!r}")


def parse_state(v):
    s = txt(v).lower()  # IN / OUT, I / O, "Check In", or the usual codes 0 and 1
    if s in ("0", "i", "3", "4") or s.endswith("in"):
        return "IN"
    if s in ("1", "o", "2", "5") or s.endswith("out"):
        return "OUT"
    return None


def pick(p, *keys):
    return next((p[k] for k in keys if p.get(k) not in (None, "")), None)


def coord(v, limit, strict):
    if v in (None, ""):
        return None
    try:
        f = float(v)
        if not -limit <= f <= limit:
            raise ValueError(f"coordinate {v!r} out of range")
    except (ValueError, TypeError):
        if strict:
            raise
        return None  # the database reader never drops a punch over a bad optional coordinate (for example the text "None")
    return f


def standardize(p, strict=False):
    """Standard punch payload -> our row. Raises KeyError/ValueError/TypeError when unusable.
    Keys (any case): emp_code and punch_time are required; log_type (IN/OUT), device, name, latitude and longitude are optional.
    strict (the API): log_type, when given, must be IN or OUT; otherwise machine codes like I/O/0/1 are understood too.
    Easy TimePro style keys (PUNCH_DATETIME, PUNCH_STATE, TERMINAL_ALIAS, FIRST_NAME) are accepted too."""
    p = {str(k).lower(): v for k, v in p.items()}
    code = txt(pick(p, "emp_code", "employee_code"))
    if not code:
        raise ValueError("emp_code missing")
    ts = parse_time(pick(p, "punch_datetime", "punch_time", "timestamp")).strftime("%Y-%m-%d %H:%M:%S")
    lt = pick(p, "log_type", "punch_state", "state")
    if strict and lt is not None and txt(lt).upper() not in ("IN", "OUT"):
        raise ValueError(f"log_type must be IN or OUT, got {lt!r}")
    return {"tx_id": f"{code}|{ts}", "emp_code": code, "punch_time": ts,
            "log_type": parse_state(lt),
            "device": txt(pick(p, "device", "device_id", "terminal_alias", "terminal_sn")),
            "latitude": coord(pick(p, "latitude", "lat"), 90, strict), "longitude": coord(pick(p, "longitude", "lng", "lon", "long"), 180, strict),
            "name": txt(pick(p, "name", "employee_name")) or " ".join(filter(None, [txt(p.get("first_name")), txt(p.get("last_name"))]))}


def ingest(punches, ts, strict=False):
    """Store punches as pending; returns (new, unreadable). A punch already stored (same employee and time) is ignored."""
    rows, bad = {}, 0
    for p in punches:  # may read a big table: do it before taking the DB write lock
        try:
            r = standardize(p, strict)
        except (KeyError, ValueError, AttributeError, TypeError):
            bad += 1
            continue
        rows[r["tx_id"]] = r
    out, state, f = [], {}, "%Y-%m-%d %H:%M:%S"
    try:
        gap = timedelta(minutes=max(int(cfg()["min_gap"]), 0))
    except ValueError:
        gap = timedelta(0)
    with db() as d:
        for r in sorted(rows.values(), key=lambda r: r["punch_time"]):
            if d.execute("SELECT 1 FROM log WHERE tx_id=?", (r["tx_id"],)).fetchone():
                continue
            if not r["log_type"]:  # ponytail: no In/Out given, so a person's day alternates IN, OUT, IN...; one missed punch flips the rest of that day
                k = (r["emp_code"], r["punch_time"][:10])
                if k not in state:  # [start of the current group, groups so far]: punches within the gap of a group's first punch share its IN/OUT
                    state[k] = [None, 0]
                    for (pt,) in d.execute("SELECT punch_time FROM log WHERE emp_code=? AND substr(punch_time,1,10)=? ORDER BY punch_time", k):
                        t = datetime.strptime(pt, f)
                        if state[k][0] is None or t - state[k][0] > gap:
                            state[k] = [t, state[k][1] + 1]
                t = datetime.strptime(r["punch_time"], f)
                if state[k][0] is None or t - state[k][0] > gap:
                    state[k] = [t, state[k][1] + 1]
                r["log_type"] = "IN" if state[k][1] % 2 else "OUT"
            out.append((r["tx_id"], r["emp_code"], r["punch_time"], r["log_type"], r["device"], r["name"] or None, r["latitude"], r["longitude"], ts, ts))
        before = d.total_changes
        d.executemany(
            "INSERT OR IGNORE INTO log(tx_id, emp_code, punch_time, log_type, device, employee_name, latitude, longitude, status, message, attempts, created, updated)"
            " VALUES(?,?,?,?,?,?,?,?,'pending','',0,?,?)", out)
        return d.total_changes - before, bad


def sync(trigger="manual", fetch=True, fetched=0):
    """Optionally pull new punches, then push every row not yet ok/duplicate (retries earlier errors)."""
    if not lock.acquire(blocking=False):
        return "Sync already running"
    try:
        c = cfg()
        started = now()
        with db() as d:
            run_id = d.execute("INSERT INTO sync_run(started, trigger) VALUES(?,?)", (started, trigger)).lastrowid
        n = {"ok": 0, "duplicate": 0, "error": 0}
        note, ok = "", True
        if fetch:
            try:
                since = datetime.strptime(c["start_date"], "%Y-%m-%d")
                if c["last_sync"]:  # also re-read a few days back: a row can land in the table late and still carry its old punch time
                    try:
                        days = max(int(c["lookback_days"]), 0)
                    except ValueError:
                        days = 1
                    since = parse_time(c["last_sync"]) - timedelta(days=days)
                new, bad = ingest(fetch_punches(c, since), started)
                fetched += new
                set_cfg(last_sync=started)
                note = f"{bad} unreadable punches skipped" if bad else ""
            except Exception as e:
                note, ok = f"Fetch failed: {e}"[:300], False
        try:
            with db() as d:
                pending = d.execute("SELECT * FROM log WHERE status IN ('pending','error')").fetchall()
            fails = 0
            for i, row in enumerate(pending):
                st, down = push_row(c, row, run_id)
                n[st] += 1
                fails = fails + 1 if down else 0
                if fails >= 3:  # ERPNext is down: don't wait out a timeout per row; the rest stay queued
                    note = (note + f" ERPNext not responding, {len(pending) - i - 1} punches left queued").strip()
                    break
        except Exception as e:
            note, ok = (note + f" Push failed: {e}").strip()[:300], False
        if ok and not n["error"]:
            set_cfg(last_ok_sync=now())  # "last sync" on the dashboard: only when every punch got through
        with db() as d:
            d.execute("UPDATE sync_run SET finished=?, fetched=?, ok=?, duplicate=?, error=?, note=? WHERE id=?",
                      (now(), fetched, n["ok"], n["duplicate"], n["error"], note, run_id))
        return f"Fetched {fetched} new, pushed {n['ok']}, duplicate {n['duplicate']}, failed {n['error']}" + (f" ({note})" if note else "")
    finally:
        lock.release()


def scheduler():
    last = 0
    while True:
        time.sleep(15)
        c = cfg()
        if c["auto_sync"] == "1" and time.time() - last >= float(c["interval_min"] or 5) * 60:
            last = time.time()
            sync("auto")


def page_arg():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except ValueError:
        return 1


def query_logs(args, paged=True):
    where, p = [], []
    if args.get("status"):
        where.append("status=?"); p.append(args["status"])
    if args.get("q"):
        where.append("(emp_code LIKE ? OR employee LIKE ? OR employee_name LIKE ? OR device LIKE ?)")
        p += [f"%{args['q']}%"] * 4
    if args.get("date_from"):
        where.append("punch_time >= ?"); p.append(args["date_from"] + " 00:00:00")
    if args.get("date_to"):
        where.append("punch_time <= ?"); p.append(args["date_to"] + " 23:59:59")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as d:
        total = d.execute("SELECT COUNT(*) FROM log" + w, p).fetchone()[0]
        sql = "SELECT * FROM log" + w + " ORDER BY punch_time DESC, tx_id DESC"
        if paged:
            sql += f" LIMIT {PER_PAGE} OFFSET {(page_arg() - 1) * PER_PAGE}"
        return d.execute(sql, p).fetchall(), total


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Attendance Sync</title>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=color-scheme content="light dark">
<link rel=icon href="{{url_for('static',filename='erpnext.svg')}}">
<style>
:root{--bg:#f4f5f6;--card:#fff;--ink:#1f272e;--mut:#6c7680;--line:#d9dee3;--soft:#f8f9fa;--blue:#0089ff;--green:#5fa32c;
--ok:#2f8f4e;--err:#d6382f;--warn:#b7791f;--wait:#7b8794}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#0c0c0c;--ink:#ececec;--mut:#9b9b9b;--line:#262626;--soft:#151515;--blue:#3aa0ff;--green:#8bc34a;
--ok:#5cc07c;--err:#ff7a70;--warn:#e0b050;--wait:#8c8c8c}}
*{box-sizing:border-box}body{margin:0;font:13px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--blue)}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
.bar{max-width:1100px;margin:0 auto;padding:0 24px;height:56px;display:flex;align-items:center;gap:24px}
.brand{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--ink);font-size:15px}
.brand img{height:28px;width:28px;border-radius:6px;display:block}.brand .arr{color:var(--mut)}.brand b{margin-left:6px;font-weight:600}
nav{display:flex;gap:2px;height:100%;flex:1}nav a{display:flex;align-items:center;padding:0 12px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent}
nav a:hover{color:var(--ink)}nav a.on{color:var(--ink);border-color:var(--blue);font-weight:600}
.state{color:var(--mut);font-size:12px;white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:6px;animation:p 1s infinite}@keyframes p{50%{opacity:.3}}
main{max-width:1100px;margin:0 auto;padding:28px 24px 56px}
.ph{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
h2{margin:0;font-size:20px;font-weight:600}h3{margin:0;font-size:14px;font-weight:600}.sub{color:var(--mut);margin:2px 0 0}
.sec{margin:32px 0 12px;display:flex;justify-content:space-between;align-items:baseline}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.num{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px 18px;color:inherit;text-decoration:none;display:block}
a.num:hover{border-color:var(--mut)}.num span{color:var(--mut)}.num b{display:block;font-size:24px;font-weight:600;margin-top:2px}
.line{color:var(--mut);margin:16px 0 0}
.tw{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow-x:auto}
table{width:100%;border-collapse:collapse}th{color:var(--mut);font-weight:500;font-size:12px;background:var(--soft)}
td,th{padding:11px 16px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}tr:last-child td{border:0}
td.wrap{white-space:normal;max-width:330px;color:var(--mut);font-size:12px}td.n,th.n{text-align:right}.red{color:var(--err);font-weight:600}
.empty{padding:36px 16px;text-align:center;color:var(--mut)}
.ind{display:inline-flex;align-items:center;gap:6px}.ind:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--wait)}
.ind.ok:before{background:var(--ok)}.ind.error:before{background:var(--err)}.ind.duplicate:before{background:var(--warn)}.ind.run:before{background:var(--blue)}
.num.ok span:before,.num.error span:before,.num.duplicate span:before,.num.pending span:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--wait)}
.num.ok span:before{background:var(--ok)}.num.error span:before{background:var(--err)}.num.duplicate span:before{background:var(--warn)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:20px 22px}
label{display:block;color:var(--mut);margin:14px 0 6px}label small{display:block;margin-top:3px;font-size:12px}
input,select,textarea{height:36px;padding:0 12px;border:1px solid var(--line);border-radius:6px;background:var(--soft);color:var(--ink);font:inherit;width:100%}
input:focus,select:focus{outline:0;border-color:var(--blue);background:var(--card)}input[readonly]{font-family:ui-monospace,Consolas,monospace;font-size:12px}
button,.btn{background:var(--blue);color:#fff;border:1px solid var(--blue);padding:7px 16px;border-radius:6px;cursor:pointer;font:inherit;text-decoration:none;display:inline-block;white-space:nowrap;line-height:20px}
button:hover,.btn:hover{opacity:.9}button:disabled{opacity:.55;cursor:default}button.sm{padding:3px 12px;font-size:12px;line-height:18px}
.btn.alt,button.alt{background:var(--card);color:var(--ink);border-color:var(--line)}
.fb{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;align-items:end;margin-bottom:16px;padding:18px 22px}.fb label{margin:0}.fb .acts{display:flex;gap:8px;flex-wrap:wrap;grid-column:1/-1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px 20px}.two>label{margin:0}.panel>.two{margin-top:14px}.stack>*+*{margin-top:20px}
.hd{display:flex;align-items:center;gap:10px;margin-bottom:6px}.hd img{height:30px;width:30px;border-radius:6px}
.bar2{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}
.msg{border:1px solid var(--line);border-left:3px solid var(--blue);background:var(--card);padding:11px 16px;border-radius:6px;margin-bottom:20px}
pre{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:12px 14px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;margin:8px 0 0}
.pg{display:flex;gap:12px 20px;align-items:center;justify-content:space-between;flex-wrap:wrap;color:var(--mut);margin-top:18px}.pgb{display:flex;gap:12px;align-items:center}
@media(max-width:760px){.bar{flex-wrap:wrap;height:auto;padding:10px 16px;gap:6px 16px}nav{order:3;flex-basis:100%;height:36px;overflow-x:auto}.state{margin-left:auto}
.nums{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}main{padding:16px}}
</style></head><body>
<header><div class=bar>
<a class=brand href="{{url_for('dash')}}"><img src="{{url_for('static',filename='mantra.svg')}}" alt="Mantra" title="Mantra"><span class=arr>→</span><img src="{{url_for('static',filename='erpnext.svg')}}" alt=ERPNext title=ERPNext><b>Attendance Sync</b></a>
<nav>{% for p,e,l in [('dash','dash','Dashboard'),('logs','logs','Punches'),('runs','runs_page','Sync history'),('console','console','Console'),('cfg','cfg_page','Settings')] %}<a href="{{url_for(e)}}" class="{{'on' if page==p}}">{{l}}</a>{% endfor %}</nav>
<span class=state>{% if running %}<span class=dot></span>Sync running…{% elif c.auto_sync=='1' %}Auto sync every {{c.interval_min}} min{% else %}Auto sync is off{% endif %}</span></div></header>
<main>{% for m in get_flashed_messages() %}<div class=msg>{{m}}</div>{% endfor %}

{% macro pager(ep, a, n, pages, total) %}{% if total %}<div class=pg><span>Showing {{(n-1)*per+1}}–{{[n*per,total]|min}} of {{total}}</span>
<span class=pgb>{% if n>1 %}<a class="btn alt" href="{{url_for(ep,**dict(a,page=n-1))}}">Previous</a>{% endif %}<span>Page {{n}} of {{pages}}</span>{% if n<pages %}<a class="btn alt" href="{{url_for(ep,**dict(a,page=n+1))}}">Next</a>{% endif %}</span></div>{% endif %}{% endmacro %}
{% macro colpick(key, label, hint) %}<label>{{label}}{% if cols %}<select name={{key}}><option value="">(none){% for n,t in cols %}<option value="{{n}}" {{'selected' if c[key]==n}}>{{n}} ({{t}}){% if key=='m_state' and values.get(n) %} – {{values[n]|join(', ')}}{% endif %}{% endfor %}</select>{% else %}<input name={{key}} value="{{c[key]}}">{% endif %}<small>{{hint}}</small></label>{% endmacro %}
{% macro ind(s) %}<span class="ind {{s}}">{{L[s]}}</span>{% endmacro %}
{% macro logtable(rows, retry=True, empty='No punches.') %}<div class=tw><table>
<tr><th>Punch time<th>Code<th>Employee<th>In/Out<th>Device<th>Location<th>Status<th class=n>Tries<th>Message<th></tr>
{% for r in rows %}<tr><td>{{r.punch_time}}<td>{{r.emp_code}}<td>{{r.employee_name or r.employee or '–'}}<td>{{r.log_type|title}}<td>{{r.device or '–'}}<td>{% if r.latitude is not none and r.longitude is not none %}<a href="https://www.google.com/maps?q={{r.latitude}},{{r.longitude}}" target=_blank rel=noopener>{{'%.5f'|format(r.latitude)}}, {{'%.5f'|format(r.longitude)}}</a>{% else %}–{% endif %}
<td>{{ind(r.status)}}<td class=n>{{r.attempts or 0}}<td class=wrap>{{r.message}}
<td>{% if retry and r.status in ('error','pending') %}<form method=post action="{{url_for('retry',tx_id=r.tx_id)}}"><button class="sm alt">Retry</button></form>{% endif %}</tr>
{% else %}<tr><td colspan=10 class=empty>{{empty}}</tr>{% endfor %}</table></div>{% endmacro %}

{% if page=='dash' %}<div class=ph><div><h2>Dashboard</h2><p class=sub>Punches are read from the Mantra database and added to ERPNext as Employee Checkins.</p></div>
<form method=post action="{{url_for('do_sync')}}"><button {{'disabled' if running}}>{{'Syncing…' if running else 'Sync now'}}</button></form></div>
<div class=nums>{% for s in ['ok','duplicate','error','pending'] %}<a class="num {{s}}" href="{{url_for('logs',status=s)}}"><span>{{L[s]}}</span><b>{{counts[s]}}</b></a>{% endfor %}</div>
<p class=line>Last fully successful sync: <b>{{c.last_ok_sync or 'never'}}</b> &nbsp;·&nbsp; last read from Mantra: {{c.last_sync or 'never'}}</p>
<div class=sec><h3>Failed punches</h3></div>{{logtable(errors, empty='No failed punches.')}}
<div class=sec><h3>Latest syncs</h3><a href="{{url_for('runs_page')}}">See all</a></div>{{runtable(runs)}}

{% elif page=='logs' %}<div class=ph><div><h2>Punches</h2><p class=sub>{{total}} record{{'' if total==1 else 's'}}</p></div></div>
<form class="fb panel" method=get>
<label>Search<input name=q value="{{a.q or ''}}" placeholder="code, name or device"></label>
<label>Status<select name=status><option value="">All{% for s in ['ok','duplicate','error','pending'] %}<option value="{{s}}" {{'selected' if a.status==s}}>{{L[s]}}{% endfor %}</select></label>
<label>From<input type=date name=date_from value="{{a.date_from or ''}}"></label>
<label>To<input type=date name=date_to value="{{a.date_to or ''}}"></label>
<div class=acts><button>Apply</button><a class="btn alt" href="{{url_for('logs')}}">Clear</a><a class="btn alt" href="{{url_for('logs_csv',**a)}}">Download CSV</a></div></form>
{{logtable(rows, empty='No punches match these filters.')}}
{{pager('logs', a, page_no, pages, total)}}

{% elif page=='runs' %}<div class=ph><div><h2>Sync history</h2><p class=sub>Every time the app read and pushed punches.</p></div></div>{{runtable(runs)}}{{pager('runs_page', a, page_no, pages, total)}}

{% elif page=='console' %}<div class=ph><div><h2>Console</h2><p class=sub>Run a read-only SELECT on the Mantra database (first {{limit}} rows are shown).</p></div></div>
<form method=post class="panel"><label style="margin-top:0">SQL<textarea name=sql spellcheck=false style="height:130px;padding:10px 12px;font:12px/1.5 ui-monospace,Consolas,monospace">{{sql}}</textarea></label>
<div class=bar2 style="margin-top:14px"><button>Run</button></div></form>
{% if err %}<div class=msg style="border-left-color:var(--err);margin:16px 0 0">{{err}}</div>{% endif %}
{% if qcols %}<div class=sec><h3>{{qrows|length}} row{{'' if qrows|length==1 else 's'}}{{' (more rows were cut off)' if cut}}</h3></div>
<div class=tw><table><tr>{% for k in qcols %}<th>{{k}}{% endfor %}</tr>{% for r in qrows %}<tr>{% for v in r %}<td>{{v}}{% endfor %}</tr>{% else %}<tr><td colspan={{qcols|length}} class=empty>No rows.</tr>{% endfor %}</table></div>{% endif %}

{% else %}<div class=ph><div><h2>Settings</h2><p class=sub>Connection details are stored in this app's local database.</p></div></div><form method=post class=stack>
<div class=two>
<section class=panel><div class=hd><img src="{{url_for('static',filename='mantra.svg')}}" alt=""><h3>Mantra database</h3></div>
<label>SQL Server<input name=m_server value="{{c.m_server}}" placeholder=".\\SQLEXPRESS"><small>Server name or SERVER\\INSTANCE. A dot means this PC.</small></label>
<label>Database<input name=m_db value="{{c.m_db}}"></label>
<label>Username<input name=m_user value="{{c.m_user}}" autocomplete=off></label>
<label>Password<input type=password name=m_pass autocomplete=new-password placeholder="{{'saved – leave blank to keep' if c.m_pass}}"></label>
<label>Table<input name=m_table value="{{c.m_table}}"></label></section>
<section class=panel><div class=hd><img src="{{url_for('static',filename='erpnext.svg')}}" alt=""><h3>ERPNext</h3></div>
<label>Site address<input name=erp_url value="{{c.erp_url}}" placeholder="http://localhost:8000"></label>
<label>API key<input name=erp_key value="{{c.erp_key}}" autocomplete=off></label>
<label>API secret<input type=password name=erp_secret autocomplete=new-password placeholder="{{'saved – leave blank to keep' if c.erp_secret}}"></label>
<label>Employee field with the punch code<input name=employee_field value="{{c.employee_field}}"><small>The punch code from the Mantra table is matched to this field on the Employee.</small></label></section></div>
<section class=panel><h3>Columns</h3><p class=sub>Tell the app which column of the table holds what. Press “Save and read columns” to list them.</p>
{% if c.m_state and values.get(c.m_state, [])|length == 1 %}<div class=msg style="border-left-color:var(--warn);margin:14px 0 0">The column “{{c.m_state}}” has only one value ({{values[c.m_state][0]}}), so every punch would be marked that way. Choose (none) for In / Out and each person’s punches will alternate In, Out, In… during the day.</div>{% endif %}
<div class=two>
{{colpick('m_emp','Employee code','Required.')}}
{{colpick('m_time','Punch date and time','Required.')}}
{{colpick('m_state','In / Out','Optional. Values like I/O, IN/OUT or 0/1. Choose (none) if the machine marks every punch as In: each person’s punches then alternate In, Out, In… during the day.')}}
<label style="color:var(--ink)"><input type=checkbox name=alt_inout value=1 {{'checked' if c.alt_inout=='1'}} onchange="document.getElementById('gapbox').style.display=this.checked?'':'none'" style="width:auto;height:auto;vertical-align:-2px;margin-right:6px"><input type=hidden name=alt_inout value=0>Alternate IN / OUT<small style="color:var(--mut)">Ignore the In / Out column and mark each person’s punches of the day IN, OUT, IN, OUT… in time order.</small></label>
<label id=gapbox style="{{'' if c.alt_inout=='1' else 'display:none'}}">Same In / Out within (minutes)<input type=number min=0 name=min_gap value="{{c.min_gap}}"><small>Punches by the same person within this many minutes of the first one get the same In / Out (09:00 In, 09:02 In, then the next punch after that is Out). Use 0 to switch it off.</small></label>
{{colpick('m_device','Device','Optional. Sent to ERPNext as the device id.')}}
{{colpick('m_lng','Longitude','Optional. Decimal degrees (-180 to 180). Choose it together with Latitude.')}}
{{colpick('m_lat','Latitude','Optional. Decimal degrees (-90 to 90). Sent to ERPNext with the check-in.')}}
{{colpick('m_name','Employee name','Optional. Only shown in the Punches list.')}}</div>
{% if sample %}<h3 style="margin-top:22px">{{sample|length}} rows of the table (the first {{sample_max}} are shown; use Console for more)</h3><div class=tw style="margin-top:8px;max-height:480px;overflow-y:auto"><table><tr>{% for k in sample[0] %}<th>{{k}}{% endfor %}</tr>{% for r in sample %}<tr>{% for v in r.values() %}<td>{{v}}{% endfor %}</tr>{% endfor %}</table></div>{% endif %}</section>
<section class=panel><h3>Schedule</h3><div class=two>
<label>Auto sync<select name=auto_sync><option value=1 {{'selected' if c.auto_sync=='1'}}>On<option value=0 {{'selected' if c.auto_sync!='1'}}>Off</select></label>
<label>Run every (minutes)<input type=number min=1 name=interval_min value="{{c.interval_min}}"></label>
<label>Re-read the last (days)<input type=number min=0 name=lookback_days value="{{c.lookback_days}}"><small>Each sync also re-reads this many days before the last sync, to catch rows that reached the table late (for example from a machine that was offline). Punches already stored are skipped. Use 0 to switch it off.</small></label>
<label>Start syncing from<input type=date name=start_date value="{{c.start_date}}"><small>If you change this date, the next sync reads everything since then again. Punches already in ERPNext are skipped.</small></label></div></section>
<section class=panel><h3>Receive punches by POST</h3><p class=sub>Any machine or script can send punches in the standard payload (a JSON list) to this address. Click it to select:</p>
<label style="margin-top:8px"><input readonly onclick="this.select()" value="{{request.host_url}}api/punches?token={{c.webhook_token}}"></label>
<pre>[{"emp_code": "1234", "punch_time": "2026-11-25 09:00:00", "log_type": "IN", "device": "Gate-1", "name": "Asha Patel", "latitude": 23.0225, "longitude": 72.5714}]</pre>
<p class=sub style="margin-top:8px">emp_code and punch_time are required. log_type (IN or OUT), device and name are optional.</p></section>
<div class=bar2><button>Save</button><button class=alt formaction="{{url_for('mantra_detect')}}">Save and read columns</button><button class=alt formaction="{{url_for('test_erp')}}">Save and test ERPNext</button></div></form>
{% endif %}
{% if running and page in ('dash','runs') %}<script>setTimeout(()=>location.reload(),3000)</script>{% endif %}
</main></body></html>"""

RUNS = """{% macro runtable(runs) %}<div class=tw><table><tr><th>#<th>Started<th>Finished<th>Started by<th class=n>New<th class=n>Pushed<th class=n>Duplicate<th class=n>Failed<th>Note</tr>
{% for r in runs %}<tr><td>{{r.id}}<td>{{r.started}}<td>{% if r.finished %}{{r.finished}}{% else %}<span class="ind run">running…</span>{% endif %}<td>{{r.trigger}}<td class=n>{{r.fetched}}<td class=n>{{r.ok}}<td class=n>{{r.duplicate}}<td class="n {{'red' if r.error}}">{{r.error}}<td class=wrap>{{r.note}}</tr>
{% else %}<tr><td colspan=9 class=empty>Nothing yet. Press “Sync now” on the dashboard.</tr>{% endfor %}</table></div>{% endmacro %}"""

TEMPLATE = PAGE.replace("{% macro ind", RUNS + "{% macro ind", 1)


def render(page, **kw):
    return render_template_string(TEMPLATE, page=page, c=cfg(), a=request.args.to_dict(), running=lock.locked(), L=LABEL, per=PER_PAGE, **kw)


def recent_runs(n):
    with db() as d:
        return d.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT ?", (n,)).fetchall()


@app.route("/")
def dash():
    with db() as d:
        counts = {s: 0 for s in ("ok", "duplicate", "error", "pending")}
        for r in d.execute("SELECT status, COUNT(*) n FROM log GROUP BY status"):
            counts[r["status"]] = r["n"]
        errors = d.execute("SELECT * FROM log WHERE status='error' ORDER BY updated DESC LIMIT 10").fetchall()
    return render("dash", counts=counts, errors=errors, runs=recent_runs(10))


@app.route("/logs")
def logs():
    rows, total = query_logs(request.args)
    return render("logs", rows=rows, total=total, page_no=page_arg(),
                  pages=max(-(-total // PER_PAGE), 1))


@app.route("/logs.csv")
def logs_csv():
    rows, _ = query_logs(request.args, paged=False)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["punch_time", "emp_code", "employee", "employee_name", "log_type", "device", "latitude", "longitude", "status", "attempts", "message", "checkin"])
    for r in rows:
        w.writerow([r["punch_time"], r["emp_code"], r["employee"], r["employee_name"], r["log_type"], r["device"],
                    r["latitude"], r["longitude"], r["status"], r["attempts"], r["message"], r["checkin"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=punch_logs.csv"})


@app.route("/runs")
def runs_page():
    n = page_arg()
    with db() as d:
        total = d.execute("SELECT COUNT(*) FROM sync_run").fetchone()[0]
        rows = d.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT ? OFFSET ?", (PER_PAGE, (n - 1) * PER_PAGE)).fetchall()
    return render("runs", runs=rows, total=total, page_no=n, pages=max(-(-total // PER_PAGE), 1))



CONSOLE_ROWS = 200
SAMPLE_ROWS = 1000  # Settings preview; ponytail: stored in config as JSON, raise only if the page stays fast
SQL_BAD = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|exec|execute|merge|grant|revoke|into|backup|restore|shutdown|waitfor|openrowset|opendatasource|xp_\w+|sp_\w+)\b", re.I)


def safe_select(sql):
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"(select|with)\b", sql, re.I) or ";" in sql or "--" in sql or "/*" in sql or SQL_BAD.search(sql):
        raise ValueError("Only one read-only SELECT is allowed (no ; comments, INSERT, UPDATE, DELETE, EXEC, INTO...)")
    return sql  # ponytail: keyword filter, not a parser; the real guard is a SQL user with SELECT-only rights (see README)


def cell(v):
    if v is None:
        return "NULL"
    return f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else str(v)[:200]


@app.route("/console", methods=["GET", "POST"])
def console():
    c = cfg()
    sql = request.form.get("sql") or (f"SELECT TOP 100 * FROM {c['m_table']}" if c["m_table"] else "")
    qcols, qrows, err, cut = [], [], "", False
    if request.method == "POST":
        try:
            with closing(Mantra(c)) as m:
                if m.kind == "mssql":
                    m.con.timeout = 30
                cur = m.run(safe_select(sql))
                qcols = [d[0] for d in cur.description]
                rows = cur.fetchmany(CONSOLE_ROWS + 1)
            cut = len(rows) > CONSOLE_ROWS
            qrows = [[cell(v) for v in r] for r in rows[:CONSOLE_ROWS]]
        except Exception as e:
            err = str(e)[:500]
    return render("console", sql=sql, qcols=qcols, qrows=qrows, err=err, cut=cut, limit=CONSOLE_ROWS)


@app.route("/api/punches", methods=["POST"])
def receive_punches():
    tok = request.headers.get("X-Token") or request.args.get("token", "")
    if not hmac.compare_digest(tok, cfg()["webhook_token"]):
        return jsonify(error="bad token"), 401
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        body = body.get("data", [body])
    if not isinstance(body, list):
        return jsonify(error="expected a JSON list of punches"), 400
    new, bad = ingest(body, now(), strict=True)
    sync("webhook", fetch=False, fetched=new)  # if a sync is already running, rows stay pending for the next one
    return jsonify(received=len(body), new=new, unreadable=bad)


@app.route("/sync", methods=["POST"])
def do_sync():
    if lock.locked():
        flash("A sync is already running")
    else:
        threading.Thread(target=sync, daemon=True).start()  # runs in background; the dashboard refreshes until it ends
        time.sleep(0.3)  # let the thread take the lock so the redirect already shows "Syncing…"
    return redirect(url_for("dash"))


@app.route("/retry/<tx_id>", methods=["POST"])
def retry(tx_id):
    with db() as d:
        row = d.execute("SELECT * FROM log WHERE tx_id=?", (tx_id,)).fetchone()
    flash(f"Retry: {push_row(cfg(), row, None)[0]}" if row else "Not found")
    return redirect(request.referrer or url_for("logs"))


def save_form():
    f = request.form.to_dict()
    for secret in ("m_pass", "erp_secret"):
        if not f.get(secret):
            f.pop(secret, None)
    if f.get("start_date") and f["start_date"] != cfg()["start_date"]:
        f["last_sync"] = ""  # new start date -> next sync re-pulls from it (already-pushed punches are skipped)
    set_cfg(**{k: v.strip() for k, v in f.items() if k in DEFAULTS})


@app.route("/config", methods=["GET", "POST"])
def cfg_page():
    if request.method == "POST":
        save_form()
        flash("Saved")
        return redirect(url_for("cfg_page"))
    c = cfg()
    return render("cfg", cols=json.loads(c["m_cols"]), sample=json.loads(c["m_sample"]), sample_max=SAMPLE_ROWS, values=json.loads(c["m_values"]))

@app.route("/mantra/detect", methods=["POST"])
def mantra_detect():
    save_form()
    c = cfg()
    try:
        with closing(Mantra(c)) as m:
            cols = m.columns()
            if not cols:
                raise ValueError(f"table {c['m_table']!r} not found or has no columns")
            sample, total = m.sample(SAMPLE_ROWS), m.count()
            values = {}
            for n, t in cols:
                if t.lower() not in DATE_TYPES:
                    try:
                        values[n] = m.distinct(n)
                    except Exception:
                        pass  # some column types (text, image) cannot be listed
        picked = {k: v for k, v in guess(cols).items() if not c[k]}
        st, note = picked.get("m_state"), ""
        if st and len(values.get(st, ())) == 1:  # a machine that marks every punch as In: that column says nothing
            del picked["m_state"]
            note = f" Column {st} has only one value ({values[st][0]}), so it is not used for In / Out: each person's punches alternate In, Out, In… instead."
        set_cfg(m_cols=json.dumps(cols), m_sample=json.dumps(sample, default=cell), m_values=json.dumps(values), **picked)
        flash(f"Connected. The table has {total} rows and {len(cols)} columns. Check the column choices below." + note)
    except Exception as e:
        flash(f"Could not read the Mantra database: {e}")
    return redirect(url_for("cfg_page"))


@app.route("/test", methods=["POST"])
def test_erp():
    save_form()
    c = cfg()
    try:
        r = http.get(c["erp_url"].rstrip("/") + "/api/method/frappe.auth.get_logged_user",
                     headers={"Authorization": f"token {c['erp_key']}:{c['erp_secret']}"}, timeout=15)
        flash(f"ERPNext: OK as {r.json()['message']}" if r.ok else f"ERPNext failed: {r.status_code} {r.text[:150]}")
    except Exception as e:
        flash(f"ERPNext failed: {e}")
    return redirect(url_for("cfg_page"))


init()
if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
