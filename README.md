# Mantra → ERPNext attendance sync

A small Flask app that reads attendance punches from the **Mantra** attendance machine's SQL Server database and adds them to **ERPNext** (HRMS) as Employee Checkins. Settings and sync history are kept in a local SQLite file, and a web page shows what was pushed and what failed.

It is the same app as the Easy TimePro version, with a database reader instead of an HTTP reader.

## What it does

- Reads new rows from the Mantra table (for example `PayTime.tmpdmpTerminalData`) on a schedule or when you press **Sync now**.
- Turns every row into the **standard punch payload** (below), saves it in SQLite first, then pushes it to ERPNext. If ERPNext is down or rejects a punch, nothing is lost: failed punches are retried on every later sync, or one by one with **Retry**.
- Skips punches ERPNext already has, so reading the same rows twice is safe.
- Only ever runs `SELECT` on the Mantra database.
- Accepts the same standard payload by `POST /api/punches`, so any other machine or script can feed ERPNext through this app.

## Standard punch payload

One punch is one JSON object. Send a list of them to `/api/punches`, or let the database reader build them.

```json
{"emp_code": "1234", "punch_time": "2026-11-25 09:00:00", "log_type": "IN", "device": "Gate-1", "name": "Asha Patel", "latitude": 23.0225, "longitude": 72.5714}
```

| Field | Required | Meaning |
|---|---|---|
| `emp_code` | yes | The employee's punch code. Matched to an Employee field in ERPNext (default `attendance_device_id`) |
| `punch_time` | yes | Local date and time. Accepted: `YYYY-MM-DD HH:MM:SS`, `YYYY-MM-DDTHH:MM:SS`, `DD-MM-YYYY HH:MM:SS`, `DD/MM/YYYY HH:MM:SS` |
| `log_type` | no | `IN` or `OUT` (any case). In the API any other value makes the punch unreadable. The database reader also understands `I`, `O`, `0`, `1`, `Check In`, `Check Out` |
| `latitude` | no | Number from -90 to 90 (also `lat`). Sent to ERPNext, which fills in the Geolocation and checks the Shift Location radius when "Allow geolocation tracking" is on in HR Settings |
| `longitude` | no | Number from -180 to 180 (also `lng`, `lon`, `long`). Send it together with `latitude` |
| `device` | no | Machine or gate name. Sent to ERPNext as the device id |
| `name` | no | Shown in the Punches list only |

Rules:

- A punch is identified by `emp_code` + `punch_time`. The same punch arriving twice is stored once.
- If "Allow geolocation tracking" is on in ERPNext, a punch without `latitude` and `longitude` fails there ("Latitude and longitude values are required"), so Mantra machine punches would fail too.
- If `log_type` is missing, a person's punches alternate **IN, OUT, IN…** during the day. One missed punch flips the rest of that day, so send `log_type` when the machine has it.
- Keys are case-insensitive. Easy TimePro style keys (`PUNCH_DATETIME`, `PUNCH_STATE`, `TERMINAL_ALIAS`, `FIRST_NAME`) work too.

`POST /api/punches?token=<token>` (or header `X-Token`) with a JSON list, or `{"data": [...]}`. The token is shown in **Settings**. The reply is `{"received": n, "new": n, "unreadable": n}`.

## Set up on the Mantra PC (Windows)

1. Install Python 3 (tick "Add to PATH") and copy this folder to the PC.
2. Right-click `install_windows.bat` and choose **Run as administrator**. It creates the virtual environment, adds a startup task (`MantraSync`) that starts the app at every boot, opens port 5001 and starts it now. Output goes to `app.log`.
3. Open http://localhost:5001 → **Settings**.
4. Under **Mantra database** enter the SQL Server (for example `.\Mantra2022`, where the dot means this PC), database, username, password and table. Press **Save and read columns**. The app lists the table's columns and its first rows, and picks the likely ones.
5. Check the **Columns** choices: employee code and punch date/time are required, In/Out, device, name, latitude and longitude are optional (choose latitude and longitude together; they are sent to ERPNext). Press **Save**.

   **If the machine records every punch as In:** the In / Out dropdown shows the values found in each column. A column with only one value (for example `IN` on every row) says nothing about In or Out, so the app does not pick it. Leave **In / Out** at `(none)`: each person's punches then alternate **In, Out, In…** during the day (first punch In, second Out, and so on). A forgotten punch flips the rest of that day, so check the Punches list now and then.
6. Under **ERPNext** enter the site address, API key and secret, and the Employee field that holds the punch code. Press **Save and test ERPNext**.
7. Set **Start syncing from**, turn **Auto sync** on, and press **Sync now** on the dashboard.

**Re-read the last (days)** (under Schedule, default 1): each sync also re-reads this many days before the last sync, to catch rows that reached the table late, for example from a machine that was offline. Set it to at least the number of days a machine can stay offline. Stored punches are skipped, so nothing is pushed twice. 0 turns it off.

To remove auto-start: `schtasks /Delete /TN MantraSync /F`. To use another port, set the `PORT` environment variable.

### Requirements

- Python 3 (tested on 3.14)
- A SQL Server ODBC driver on the PC. The old `SQL Server` driver ships with Windows; `ODBC Driver 17` or `18 for SQL Server` is better and is picked automatically when installed.
- ERPNext with the **HRMS** app, and an API key and secret for a user who can create Employee Checkins.
- To read from another PC instead of the Mantra PC, use `PCNAME\INSTANCE` as the server and enable TCP/IP and the SQL Browser service for that instance.

### Use a read-only SQL login

The app only reads. Instead of `sa`, create a login that can do just that:

```sql
CREATE LOGIN attsync WITH PASSWORD = 'choose-a-strong-password';
USE PayTime;
CREATE USER attsync FOR LOGIN attsync;
GRANT SELECT ON dbo.tmpdmpTerminalData TO attsync;
```

### About `tmpdmpTerminalData`

The name suggests a temporary dump table that Mantra's software may refill or clear. Every row the app reads is stored in `attendance.db` straight away, so nothing is lost when Mantra clears it, but punches that are cleared before the app reads them are gone. Keep **Auto sync** on, every few minutes.

## Tests

    python test_app.py

## Notes

- The site has no login. Run it on a trusted network only.
- The SQL password and ERPNext secret are saved in plain text in `attendance.db`, which is not part of the repository.
- A punch is matched only to an **Active** Employee. Punches for an unknown or inactive code show as Failed ("No active Employee…") and are retried on every sync until the Employee is fixed.
- For the usual Mantra table `dbo.tmpDmpTerminalData`, **Save and read columns** picks `CardNO` (employee code), `In_Out_Time` (punch time), `Mode` (In/Out), `DeviceCode`, `Latitude` and `Longitude`. `EmpCode` and `EmpID` are 0 in that table, so they are not used as the employee code. Check the choices anyway. If a value in your table needs special handling (for example an In/Out code other than the ones listed), change `parse_state` in `app.py`.
- The ERPNext logo in `static/` belongs to its owner. The Mantra icon is a generic fingerprint drawing, not Mantra's logo.
