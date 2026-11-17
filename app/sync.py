"""Daily download from the sonnen portal + scheduling."""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, ingest
from .sonnen_portal import PortalError, SonnenPortal

log = logging.getLogger("sonnen.sync")
_lock = threading.Lock()
state = {"running": False}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def credentials_configured() -> bool:
    return bool(config.SONNEN_USERNAME and config.SONNEN_PASSWORD)


def run_sync(trigger: str = "manual") -> dict:
    """Import new files from IMPORT_DIR, then pull fresh data from the portal. Never raises."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "Synchronisation läuft bereits."}
    state["running"] = True
    with db.connect() as conn:
        log_id = conn.execute("INSERT INTO sync_log(started_at, trigger) VALUES(?,?)",
                              (_now_iso(), trigger)).lastrowid
    ok, parts = True, []
    try:
        with db.connect() as conn:
            for res in ingest.import_folder(conn):
                parts.append(f"{res['file']}: {res.get('written', 0)} Zeilen" if res["kind"] != "error"
                             else f"{res['file']}: {res.get('error')}")
        if credentials_configured():
            parts += _pull_portal()
        else:
            parts.append("Portal-Download übersprungen (keine Zugangsdaten konfiguriert).")
    except PortalError as exc:
        ok = False
        parts.append(f"Portal: {exc}")
    except Exception as exc:  # network errors etc. - report, keep the app alive
        ok = False
        parts.append(f"Fehler: {type(exc).__name__}: {exc}")
        log.exception("sync failed")
    finally:
        message = " · ".join(parts) or "Nichts zu tun."
        with db.connect() as conn:
            conn.execute("UPDATE sync_log SET finished_at=?, ok=?, message=? WHERE id=?",
                         (_now_iso(), int(ok), message, log_id))
        state["running"] = False
        _lock.release()
    log.info("sync (%s) %s: %s", trigger, "ok" if ok else "FAILED", message)
    return {"ok": ok, "message": message}


def _pull_portal() -> list[str]:
    with db.connect() as conn:
        refresh = db.kv_get(conn, "sonnen_refresh_token")
        site_id = config.SONNEN_SITE_ID or db.kv_get(conn, "sonnen_site_id")
        backfilled = db.kv_get(conn, "backfill_done") == "1"
    portal = SonnenPortal(config.SONNEN_USERNAME, config.SONNEN_PASSWORD, refresh)
    parts = []
    try:
        portal.authenticate()
        if not site_id:
            sites = portal.sites()
            if not sites:
                raise PortalError("Im Portal-Konto wurde keine Anlage gefunden.")
            site_id = sites[0]["id"]
            if len(sites) > 1:
                parts.append(f"{len(sites)} Anlagen im Konto, verwende die erste "
                             f"(sonst SONNEN_SITE_ID setzen: {', '.join(s['id'] for s in sites)})")
        now = datetime.now(config.TZ)
        # Normally only the current year (plus the old year in January). The very first
        # run walks back year by year until the portal has nothing more.
        years = [now.year] + ([now.year - 1] if now.month == 1 or not backfilled else [])
        written = 0
        year = None
        todo = list(years)
        while todo:
            year = todo.pop(0)
            start = datetime(year, 1, 1, tzinfo=config.TZ)
            end = datetime(year + 1, 1, 1, tzinfo=config.TZ) - timedelta(seconds=1)
            got = 0
            try:
                for text in (portal.statistics_csv(site_id, start, end, "1-day"),
                             portal.measurements_csv(site_id, start, end, "3-hours")):
                    with db.connect() as conn:
                        got += ingest.import_csv(conn, text, source="portal")["rows"]
            except (PortalError, ValueError):
                if year == now.year:
                    raise
                break  # the portal has nothing (or refuses) for years before the installation
            written += got
            if not backfilled and not todo and got > 0 and year > now.year - 15:
                todo.append(year - 1)
        # The last 30 days are available hourly - keep them, they make comparisons at
        # arbitrary times of day exact.
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        try:
            for text in (portal.statistics_csv(site_id, start, end, "1-hour"),
                         portal.measurements_csv(site_id, start, end, "1-hour")):
                with db.connect() as conn:
                    written += ingest.import_csv(conn, text, source="portal")["rows"]
        except (PortalError, ValueError) as exc:
            parts.append(f"Stundenwerte nicht verfügbar ({exc})")
        parts.insert(0, f"Portal: {written} Zeilen geladen")
        with db.connect() as conn:
            db.kv_set(conn, "backfill_done", "1")
            db.kv_set(conn, "sonnen_site_id", site_id)
    finally:
        with db.connect() as conn:
            db.kv_set(conn, "sonnen_refresh_token", portal.refresh_token)
        portal.close()
    return parts


def last_runs(conn, limit: int = 15) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]


def _seconds_until_next() -> float:
    hh, mm = (int(x) for x in config.SYNC_TIME.split(":"))
    now = datetime.now(config.TZ)
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def scheduler() -> None:
    """Sync on startup when the last successful run is older than 20 h, then once a day."""
    await asyncio.sleep(2)
    with db.connect() as conn:
        row = conn.execute("SELECT finished_at FROM sync_log WHERE ok=1 AND trigger!='import'"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    stale = True
    if row and row["finished_at"]:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["finished_at"])
        stale = age > timedelta(hours=20)
    if stale:
        await asyncio.to_thread(run_sync, "start")
    else:  # still pick up files dropped into the import folder
        with db.connect() as conn:
            ingest.import_folder(conn)
    while True:
        await asyncio.sleep(_seconds_until_next())
        await asyncio.to_thread(run_sync, "täglich")
        await asyncio.sleep(60)
