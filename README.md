# Sonnen-Abgleich

Small web app that keeps a local copy of the data of a sonnen battery/PV system,
shows statistics on it, and compares sonnen's recorded grid purchase with readings
taken off the official meter of the grid operator.

The UI is in German (it is used by the house owner); code and docs are in English.

## What it does

- **Daily download** from the sonnen customer portal (my.sonnen.de), plus manual
  import of the portal's CSV exports (`statistics_*.csv`, `measurements_*.csv`).
- **Übersicht / Analyse**: grid purchase per month across years, where the consumed
  energy came from (PV / battery / grid), calendar heat map, per day/week/month
  tables, night-time base load, a list of days that stick out, and the power
  profile of any day.
- **Zählerabgleich**: readings of the official meter (OBIS 1.8.0 import, optionally
  2.8.0 export) are entered with date and time. For every pair of consecutive
  readings the app compares the meter's difference with what sonnen recorded in
  exactly that time span.
- **Meter swaps**: every reading belongs to a meter identified by its number. A swap
  is recorded with its date, the final value of the old meter and the first value of
  the new one. Values of different devices are never subtracted from each other.
- **Data gaps**: periods where sonnen recorded nothing (missing or all-zero rows) are
  detected, excluded from averages, shown in every chart, and an estimate of the
  unrecorded grid purchase is given, based on the same calendar week of another year.

## Run locally

```sh
cp .env.example .env        # fill in portal login and an app password
docker compose up -d --build
open http://localhost:8080
```

CSV exports lying in the project folder are imported automatically (the folder is
mounted read-only as the import folder). The database is `data/sonnen.sqlite`.

Without Docker:

```sh
pip install -r requirements.txt
IMPORT_DIR=. DATA_DIR=./data uvicorn app.main:app --reload
python -m pytest            # tests
```

## Configuration

| Variable | Meaning |
|---|---|
| `SONNEN_USERNAME`, `SONNEN_PASSWORD` | Login of my.sonnen.de. Empty = no download, CSV import only. |
| `SONNEN_SITE_ID` | Only if the account has several installations (the sync log lists the ids). |
| `APP_PASSWORD` | Password for this app. Empty = no login at all. |
| `SITE_NAME` | Text under the title, e.g. the address. |
| `SYNC_TIME` | Daily download time, house local time (`04:20`). |
| `SYNC_ENABLED` | `0` switches the scheduler off. |
| `DATA_DIR` | Folder of the SQLite file (`/data` in the image). |
| `IMPORT_DIR` | Folder scanned for `*.csv` on start and before each sync (`$DATA_DIR/import`). |
| `SITE_TZ` | Time zone of the house (`Europe/Berlin`). |

`SONNEN_PASSWORD` and `APP_PASSWORD` can also be given as `…_FILE` pointing to a
file, which is how Docker secrets are mounted.

## Docker Swarm

`deploy/` holds a stack for a swarm that already runs
[caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy) as its edge proxy
(same pattern as pforte's `examples/swarm`). Everything is run from the machine that holds
`deploy/.env` and `deploy/secrets`; the Docker CLI reaches the swarm over SSH.

```sh
cd deploy
./setup.sh            # makes secrets/app_password and .env; asks for the sonnen portal login
$EDITOR .env          # APP_HOST, SWARM_MANAGER, DATA_NODE, DATA_DIR
./deploy.sh --check   # validates and says what would be deployed; touches nothing
./deploy.sh
```

- `APP_HOST` needs a DNS record pointing at the edge proxy before the first deploy; the proxy
  gets the TLS certificate for it.
- `IMAGE=build` builds this checkout on the node that runs it, named after its content. Nothing
  is pushed to a registry. To update, change the code and run `./deploy.sh` again.
- The database is one SQLite file in `DATA_DIR` on `DATA_NODE`: one replica, pinned to that node
  by its ID, stopped before the new one starts. Backup = copy of that folder.
- Secrets (`app_password`, and optionally `sonnen_username` + `sonnen_password`) are swarm
  secrets named after a fingerprint of their file, so changing a file and running `./deploy.sh`
  replaces them. Without the portal login the app runs on CSV import only.
- The container has a read-only filesystem, no Linux capabilities and an unprivileged user.
- The sign-in password is in `deploy/secrets/app_password`.
- `./upload-csv.py --status` shows what the deployed app holds and the last sync runs;
  `./upload-csv.py` uploads the CSV exports of the project folder (only needed without the
  portal login, or to fill a period the portal no longer offers).

```sh
export DOCKER_HOST=ssh://<SWARM_MANAGER>
docker stack services sonnen
docker service logs -f sonnen_app
```

## How the portal download works, and its limits

sonnen offers no documented public API. `app/sonnen_portal.py` uses the same
endpoints as the portal's own web page: the OAuth login form on account.sonnen.de
(authorization code + PKCE) and the CSV endpoints behind the portal's download
button on my-api.sonnen.de.

- The login form and the endpoints were verified against the live portal, and a
  wrong password is reported cleanly. **A successful login could not be tested
  during development** (no credentials) - check the first run under "Daten".
- If sonnen changes the portal, the download fails with a message in the sync log;
  the app keeps working and CSV files can still be imported by hand.
- Each run loads the current year (daily energy + 3-hour power; on the very first
  run also earlier years) and the last 30 days in hourly resolution. Hourly data is
  only offered by the portal for the last 30 days, so it accumulates from the first
  sync on. Imports are idempotent, and an empty row from the portal never overwrites
  existing data.
- The refresh token is stored in the database so the app does not log in with the
  password every day.

## How the comparison is calculated

For two readings at `t1` and `t2` of the same meter:

- whole days in between use sonnen's daily totals,
- the partial days at both ends use hourly energy if available, else the 3-hour
  power averages, else the daily total spread evenly (marked with `≈`),
- time without sonnen data is counted (`Tage ohne Daten`) and estimated in three
  scenarios from the same week of another year: system ran normally (low), battery
  out but PV running (middle), PV and battery both off (high),
- verdict "stimmt überein" within 3 % or 2 kWh.

A day counts as "no data" when its row is missing or everything in it is zero - a
house never consumes exactly 0 Wh.

## Layout

```
app/main.py           API, login, static files
app/analysis.py       coverage, gaps, estimates, anomalies, meter comparison
app/ingest.py         CSV import
app/sonnen_portal.py  portal login + download
app/sync.py           daily scheduler, sync log
app/db.py             SQLite schema
app/static/           UI (plain JS + ECharts, no build step)
tests/                pytest
```

## License

Copyright 2026 Michael Vogt. Licensed under the [Apache License, Version 2.0](LICENSE).

`app/static/vendor/echarts.min.js` is [Apache ECharts](https://echarts.apache.org/), also under
the Apache License 2.0.
