#!/usr/bin/env python3
"""Uploads sonnen CSV exports to the deployed app and shows what the app holds afterwards.

    ./upload-csv.py                  # every *.csv in the project folder
    ./upload-csv.py a.csv b.csv      # these files
    ./upload-csv.py --status         # upload nothing, only show the state of the app

Reads APP_HOST from .env and the password from secrets/app_password. Uploading the same file again is
harmless: days that exist are updated, nothing is counted twice.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

HERE = Path(__file__).resolve().parent


def setting(name: str) -> str:
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit(f"{name} is not set in .env")


BASE = f"https://{setting('APP_HOST')}"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(path: str, data: bytes | None = None, content_type: str = "application/json"):
    request = urllib.request.Request(BASE + path, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with opener.open(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        sys.exit(f"{path}: HTTP {error.code} - {detail}")


def main() -> None:
    args = sys.argv[1:]
    status_only = args == ["--status"]
    files = [] if status_only else [Path(a) for a in args] or sorted(HERE.parent.glob("*.csv"))
    password = (HERE / "secrets" / "app_password").read_text(encoding="utf-8").strip()
    call("/api/login", json.dumps({"password": password}).encode())

    for path in files:
        result = call("/api/import?name=" + urllib.parse.quote(path.name), path.read_bytes(), "text/csv")
        print(f"{path.name}: {result['written']} of {result['rows']} rows taken "
              f"({result['kind']}, {result['resolution']}, {result['first']} to {result['last']})")

    o = call("/api/overview")
    c = o["counts"]
    print(f"\n{BASE} now holds: {o['first_day']} to {o['last_day']} (data up to {o['data_end']})")
    print(f"  {c['daily']} daily totals, {c['power']} power intervals, {c['energy_fine']} hourly energy rows, "
          f"{c['meters']} meters, {c['readings']} readings")
    for gap in o["gaps"]:
        print(f"  no sonnen data: {gap['from']} to {gap['to']} ({gap['missing_days']} days)")
    sync = o["sync"]
    print(f"  portal login configured: {'yes' if sync['configured'] else 'no'}; sync running: {'yes' if sync['running'] else 'no'}")
    for run in sync["runs"][:4]:
        state = "running" if run["ok"] is None else "ok" if run["ok"] else "FAILED"
        print(f"  {run['started_at']} [{run['trigger']}] {state}: {run['message'] or ''}")


if __name__ == "__main__":
    main()
