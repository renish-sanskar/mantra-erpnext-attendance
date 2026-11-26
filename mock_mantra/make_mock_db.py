"""Builds a fake Mantra database (SQLite) so the app can be tried without SQL Server.

Usage:  python mock_mantra/make_mock_db.py [file]      (default: mock_mantra/PayTime.db)
Then in Settings: SQL Server = sqlite:<full path to that file>, Table = tmpdmpTerminalData.

The column names below are placeholders, not Mantra's real ones. Use "Save and read columns" and map them.
"""
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

STAFF = {"1001": "Asha Patel", "1002": "Ravi Shah", "1003": "Meera Joshi", "1004": "Kiran Desai", "1005": "Neha Mehta",
         "1006": "Arjun Rao", "1007": "Pooja Nair", "1008": "Sanjay Iyer", "1009": "Divya Menon", "1010": "Vikram Singh"}


def create(path):
    if os.path.exists(path):
        os.remove(path)
    now = datetime.now()
    day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = []
    while day.date() <= now.date():
        if day.weekday() != 6:  # Sunday off
            for code, name in STAFF.items():
                r = random.Random(f"{day:%Y%m%d}{code}")
                if r.random() < 0.08:  # absent
                    continue
                for hour, spread, mode in ((9, (-25, 40), "I"), (18, (-20, 60), "O")):
                    t = day.replace(hour=hour) + timedelta(minutes=r.randint(*spread))
                    if t <= now:
                        rows.append((code, name, t.strftime("%Y-%m-%d %H:%M:%S"), mode, "Gate-1"))
        day += timedelta(days=1)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE tmpdmpTerminalData (EmployeeCode TEXT, EmployeeName TEXT, PunchDateTime DATETIME, InOutMode TEXT, TerminalName TEXT)")
        con.executemany("INSERT INTO tmpdmpTerminalData VALUES (?,?,?,?,?)", rows)
    return len(rows)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "PayTime.db")
    print(f"{create(out)} punches written to {out}")
