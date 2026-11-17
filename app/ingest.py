"""Import of sonnen portal CSV exports (the same files the portal's download button produces).

Two file kinds exist:
  statistics_*.csv    energy per interval in Wh   (1-day or 1-hour resolution)
  measurements_*.csv  average power per interval W (3-hours or 1-hour resolution)
"""
import csv
import io
from datetime import datetime, timezone
from statistics import median

from . import config

STAT_COLS = ["produced_energy", "consumed_energy", "battery_charged_energy",
             "battery_discharged_energy", "grid_feedin_energy", "grid_purchase_energy"]
MEAS_COLS = ["production", "consumption", "battery_charge", "battery_discharge",
             "grid_feedin", "grid_consumption"]


MAX_DUR = 2 * 86400  # no interval is longer than this; bounds the overlap lookups


class ImportError_(ValueError):
    pass


def _num(value) -> int:
    if value is None or value == "":
        return 0
    return int(round(float(value)))


def _parse(text: str):
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if "timestamp" not in header:
        raise ImportError_("Keine sonnen-CSV: Spalte 'timestamp' fehlt.")
    if all(c in header for c in STAT_COLS):
        kind = "statistics"
    elif all(c in header for c in MEAS_COLS):
        kind = "measurements"
    else:
        raise ImportError_("Unbekanntes CSV-Format (weder statistics noch measurements).")
    rows = []
    for raw in reader:
        stamp = (raw.get("timestamp") or "").strip()
        if not stamp:
            continue
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=config.TZ)
        rows.append((ts, raw))
    rows.sort(key=lambda r: r[0])
    return kind, rows


def _durations(rows) -> tuple[int, list[int]]:
    """Nominal interval length of the file and the length of each row in seconds.

    Rows normally last until the next row (which handles the 2 h / 4 h rows at DST
    changes); across a hole in the data a row only counts for the nominal length.
    """
    gaps = [int((b[0] - a[0]).total_seconds()) for a, b in zip(rows, rows[1:])]
    nominal = int(median(gaps)) if gaps else 86400
    durs = [g if 0 < g <= nominal * 1.5 else nominal for g in gaps] + [nominal]
    return nominal, durs


def _is_zero(values) -> bool:
    return not any(values)


def import_csv(conn, text: str, source: str = "upload") -> dict:
    kind, rows = _parse(text)
    if not rows:
        return {"kind": kind, "rows": 0, "written": 0, "resolution": None, "first": None, "last": None}
    nominal, durs = _durations(rows)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written = 0

    if kind == "statistics" and nominal >= 20 * 3600:
        resolution = "1-day"
        for ts, raw in rows:
            day = ts.astimezone(config.TZ).date().isoformat()
            vals = [_num(raw[c]) for c in STAT_COLS]
            if _is_zero(vals):
                # Never let an empty row from the portal wipe out data we already have.
                old = conn.execute("SELECT consumed, produced, purchase FROM daily WHERE day=?", (day,)).fetchone()
                if old and any(old):
                    continue
            conn.execute(
                "INSERT INTO daily(day,produced,consumed,bat_charged,bat_discharged,feedin,purchase,source,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET produced=excluded.produced,"
                " consumed=excluded.consumed, bat_charged=excluded.bat_charged, bat_discharged=excluded.bat_discharged,"
                " feedin=excluded.feedin, purchase=excluded.purchase, source=excluded.source, updated_at=excluded.updated_at",
                (day, *vals, source, now),
            )
            written += 1
    else:
        table = "energy_fine" if kind == "statistics" else "power"
        resolution = f"{nominal // 60} min" if nominal < 3600 else f"{nominal // 3600} h"
        for (ts, raw), dur in zip(rows, durs):
            epoch = int(ts.timestamp())
            # Finer resolution wins: skip if finer rows already cover this interval,
            # otherwise remove coarser/overlapping rows before inserting.
            finer = conn.execute(
                f"SELECT 1 FROM {table} WHERE ts > ? AND ts < ? AND ts + dur > ? AND dur < ? LIMIT 1",
                (epoch - MAX_DUR, epoch + dur, epoch, int(dur * 0.7)),
            ).fetchone()
            if finer:
                continue
            if kind == "statistics":
                vals = [_num(raw[c]) for c in STAT_COLS]
            else:
                vals = [_num(raw[c]) for c in MEAS_COLS]
            if _is_zero(vals):
                old = conn.execute(f"SELECT * FROM {table} WHERE ts=?", (epoch,)).fetchone()
                if old and any(old[k] for k in old.keys() if k not in ("ts", "dur", "soc")):
                    continue
            conn.execute(f"DELETE FROM {table} WHERE ts > ? AND ts < ? AND ts + dur > ?",
                         (epoch - MAX_DUR, epoch + dur, epoch))
            if kind == "statistics":
                conn.execute(
                    "INSERT INTO energy_fine(ts,dur,produced,consumed,bat_charged,bat_discharged,feedin,purchase)"
                    " VALUES(?,?,?,?,?,?,?,?)", (epoch, dur, *vals))
            else:
                soc = raw.get("battery_state_of_charge")
                direct = raw.get("direct_consumption")
                conn.execute(
                    "INSERT INTO power(ts,dur,production,consumption,bat_charge,bat_discharge,feedin,purchase,soc,direct)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (epoch, dur, *vals, _num(soc) if soc not in (None, "") else None,
                     _num(direct) if direct not in (None, "") else None))
            written += 1

    return {
        "kind": kind, "resolution": resolution, "rows": len(rows), "written": written,
        "first": rows[0][0].astimezone(config.TZ).isoformat(timespec="minutes"),
        "last": rows[-1][0].astimezone(config.TZ).isoformat(timespec="minutes"),
    }


def import_folder(conn) -> list[dict]:
    """Import every not-yet-seen *.csv from IMPORT_DIR. Returns one result per new file."""
    results = []
    folder = config.IMPORT_DIR
    if not folder.is_dir():
        return results
    for path in sorted(folder.glob("*.csv")):
        stat = path.stat()
        key = (path.name, stat.st_size, int(stat.st_mtime))
        if conn.execute("SELECT 1 FROM imports WHERE name=? AND size=? AND mtime=?", key).fetchone():
            continue
        try:
            res = import_csv(conn, path.read_text(encoding="utf-8"), source=f"file:{path.name}")
        except (ImportError_, ValueError, KeyError) as exc:
            res = {"kind": "error", "rows": 0, "written": 0, "error": str(exc)}
        res["file"] = path.name
        conn.execute("INSERT OR REPLACE INTO imports(name,size,mtime,rows,imported_at) VALUES(?,?,?,?,?)",
                     (*key, res.get("written", 0), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        results.append(res)
    return results
