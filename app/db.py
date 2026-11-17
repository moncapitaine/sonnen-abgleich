"""SQLite storage. One small file, safe to back up by copying the data volume."""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
-- Daily energy totals from the sonnen "statistics" export, in Wh. Key = local date.
CREATE TABLE IF NOT EXISTS daily (
    day TEXT PRIMARY KEY,
    produced INTEGER NOT NULL, consumed INTEGER NOT NULL,
    bat_charged INTEGER NOT NULL, bat_discharged INTEGER NOT NULL,
    feedin INTEGER NOT NULL, purchase INTEGER NOT NULL,
    source TEXT, updated_at TEXT
);
-- Sub-daily energy (hourly statistics export), Wh per interval. ts = UTC epoch of interval start.
CREATE TABLE IF NOT EXISTS energy_fine (
    ts INTEGER PRIMARY KEY, dur INTEGER NOT NULL,
    produced INTEGER NOT NULL, consumed INTEGER NOT NULL,
    bat_charged INTEGER NOT NULL, bat_discharged INTEGER NOT NULL,
    feedin INTEGER NOT NULL, purchase INTEGER NOT NULL
);
-- Average power per interval from the "measurements" export, in W.
CREATE TABLE IF NOT EXISTS power (
    ts INTEGER PRIMARY KEY, dur INTEGER NOT NULL,
    production INTEGER NOT NULL, consumption INTEGER NOT NULL,
    bat_charge INTEGER NOT NULL, bat_discharge INTEGER NOT NULL,
    feedin INTEGER NOT NULL, purchase INTEGER NOT NULL,
    soc INTEGER, direct INTEGER
);
-- Official meters of the grid operator. A meter swap closes one row and opens the next.
CREATE TABLE IF NOT EXISTS meters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL,
    label TEXT,
    installed_at INTEGER,
    removed_at INTEGER,
    note TEXT
);
-- Readings taken off the official meter. import = OBIS 1.8.0 (Bezug), export = OBIS 2.8.0 (Einspeisung), kWh.
-- kind: 'install' (first value of a new meter), 'reading', 'removal' (final value of a replaced meter).
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    taken_at INTEGER NOT NULL,
    import_kwh REAL NOT NULL,
    export_kwh REAL,
    kind TEXT NOT NULL DEFAULT 'reading',
    note TEXT,
    created_at TEXT,
    UNIQUE (meter_id, taken_at)
);
-- Prices of the electricity contract. valid_from is a local date; '' means "from the beginning".
-- A price change is a second row with the date it applies from.
CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    valid_from TEXT NOT NULL DEFAULT '',
    base_eur_month REAL NOT NULL,
    price_ct_kwh REAL NOT NULL,
    UNIQUE (valid_from)
);
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL, finished_at TEXT,
    trigger TEXT, ok INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    name TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
    rows INTEGER, imported_at TEXT,
    PRIMARY KEY (name, size, mtime)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
    else:
        conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
