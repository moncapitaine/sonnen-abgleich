"""Web app: JSON API + static single-page UI."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, config, db, ingest, sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
SESSION_DAYS = 90
STARTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BUILD = {"revision": config.REVISION, "built_at": config.BUILT_AT, "started_at": STARTED_AT}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    with db.connect() as conn:
        if not db.kv_get(conn, "session_secret"):
            db.kv_set(conn, "session_secret", secrets.token_hex(32))
        ingest.import_folder(conn)
    task = asyncio.create_task(sync.scheduler()) if config.SYNC_ENABLED else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Sonnen-Abgleich", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---- login ---------------------------------------------------------------------

def _sign(expires: int) -> str:
    with db.connect() as conn:
        key = db.kv_get(conn, "session_secret", "")
    return hmac.new(key.encode(), str(expires).encode(), hashlib.sha256).hexdigest()


def _authenticated(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    try:
        expires, sig = request.cookies.get("sid", "").split(".", 1)
        return int(expires) > time.time() and hmac.compare_digest(sig, _sign(int(expires)))
    except ValueError:
        return False


def require_login(request: Request) -> None:
    if not _authenticated(request):
        raise HTTPException(401, "Bitte anmelden.")


class Login(BaseModel):
    password: str


@app.get("/api/health")
def health():
    # Public on purpose: lets anyone with the address check which build answers, nothing more.
    return {"ok": True, **BUILD}


@app.get("/api/session")
def session(request: Request):
    ok = _authenticated(request)
    return {"auth_required": bool(config.APP_PASSWORD), "authenticated": ok,
            "site_name": config.SITE_NAME if ok else ""}


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    if not config.APP_PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(body.password.encode(), config.APP_PASSWORD.encode()):
        time.sleep(1.5)  # slow down guessing
        raise HTTPException(401, "Falsches Passwort.")
    expires = int(time.time()) + SESSION_DAYS * 86400
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie("sid", f"{expires}.{_sign(expires)}", max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=https)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("sid")
    return {"ok": True}


api = Depends(require_login)


# ---- sonnen data ------------------------------------------------------------------

@app.get("/api/overview", dependencies=[api])
def overview():
    with db.connect() as conn:
        ds = analysis.Dataset(conn)
        runs = sync.last_runs(conn)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("daily", "power", "energy_fine", "readings", "meters")}
        fine_from = conn.execute("SELECT MIN(ts) FROM energy_fine").fetchone()[0]
    return {
        "first_day": ds.first_day.isoformat() if ds.first_day else None,
        "last_day": ds.last_day.isoformat() if ds.last_day else None,
        "data_end": analysis.local_iso(ds.data_end),
        "hourly_from": analysis.local_iso(fine_from),
        "gaps": ds.gaps(),
        "counts": counts,
        "build": BUILD,
        "sync": {
            "configured": sync.credentials_configured(), "enabled": config.SYNC_ENABLED,
            "time": config.SYNC_TIME, "running": sync.state["running"], "runs": runs,
        },
    }


@app.get("/api/daily", dependencies=[api])
def daily():
    with db.connect() as conn:
        return analysis.Dataset(conn).daily_table()


@app.get("/api/power", dependencies=[api])
def power(start: date, end: date):
    if end < start or (end - start).days > 400:
        raise HTTPException(422, "Zeitraum ungültig (max. 400 Tage).")
    t1, _ = analysis.day_bounds(start)
    _, t2 = analysis.day_bounds(end)
    cols = {k: [] for k in ("t", "dur", "production", "consumption", "bat_charge",
                            "bat_discharge", "feedin", "purchase", "soc")}
    with db.connect() as conn:
        for r in conn.execute("SELECT * FROM power WHERE ts >= ? AND ts < ? ORDER BY ts", (t1, t2)):
            cols["t"].append(analysis.local_iso(r["ts"]))
            for k in list(cols)[1:]:
                cols[k].append(r[k])
    return cols


@app.post("/api/import", dependencies=[api])
async def import_file(request: Request, name: str = "upload.csv"):
    raw = await request.body()
    if len(raw) > 30_000_000:
        raise HTTPException(413, "Datei zu groß.")
    try:
        text = raw.decode("utf-8-sig")
        with db.connect() as conn:
            res = ingest.import_csv(conn, text, source=f"upload:{name[:80]}")
            conn.execute("INSERT INTO sync_log(started_at, finished_at, trigger, ok, message) VALUES(?,?,?,?,?)",
                         (sync._now_iso(), sync._now_iso(), "import", 1,
                          f"{name[:80]}: {res['written']} von {res['rows']} Zeilen übernommen ({res['kind']}, {res['resolution']})"))
    except (ingest.ImportError_, ValueError, KeyError, UnicodeDecodeError) as exc:
        raise HTTPException(422, f"Import fehlgeschlagen: {exc}")
    return res


@app.post("/api/sync", dependencies=[api])
def start_sync():
    if sync.state["running"]:
        return {"started": False, "message": "Synchronisation läuft bereits."}
    threading.Thread(target=sync.run_sync, args=("manuell",), daemon=True).start()
    return {"started": True}


# ---- official meters and readings ---------------------------------------------------

class MeterIn(BaseModel):
    number: str = Field(min_length=1, max_length=60)
    label: str | None = Field(default=None, max_length=120)
    installed_at: str | None = None
    removed_at: str | None = None      # empty = still in service
    note: str | None = Field(default=None, max_length=500)
    start_import_kwh: float | None = Field(default=None, ge=0)
    start_export_kwh: float | None = Field(default=None, ge=0)


class ReadingIn(BaseModel):
    meter_id: int
    taken_at: str
    import_kwh: float = Field(ge=0)
    export_kwh: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)


class MeterChange(BaseModel):
    old_meter_id: int
    changed_at: str
    # The final values of the old meter are required, unless they are explicitly declared unknown
    # (a swap in the past whose protocol is gone).
    old_final_unknown: bool = False
    old_final_import_kwh: float | None = Field(default=None, ge=0)
    old_final_export_kwh: float | None = Field(default=None, ge=0)
    new_number: str = Field(min_length=1, max_length=60)
    new_start_import_kwh: float = Field(ge=0)
    new_start_export_kwh: float = Field(ge=0)
    note: str | None = Field(default=None, max_length=500)


def _when(value: str) -> int:
    try:
        epoch = analysis.parse_local(value)
    except ValueError:
        raise HTTPException(422, "Datum/Uhrzeit nicht lesbar.")
    if epoch > time.time() + 600:
        raise HTTPException(422, "Der Zeitpunkt liegt in der Zukunft.")
    return epoch


def _fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%d.%m.%Y %H:%M")


def _check_reading(conn, meter_id: int, taken_at: int, imp: float, exp: float | None, skip_id: int | None = None):
    """A meter only counts upwards - reject values that contradict neighbouring readings."""
    meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
    if not meter:
        raise HTTPException(404, "Zähler nicht gefunden.")
    if meter["installed_at"] and taken_at < meter["installed_at"]:
        raise HTTPException(422, f"Zähler {meter['number']} wurde erst am {_fmt(meter['installed_at'])} eingebaut.")
    if meter["removed_at"] and taken_at > meter["removed_at"]:
        raise HTTPException(422, f"Zähler {meter['number']} wurde am {_fmt(meter['removed_at'])} ausgebaut. "
                                 "Bitte den aktuellen Zähler wählen.")
    skip = skip_id or -1
    prev = conn.execute("SELECT * FROM readings WHERE meter_id=? AND taken_at<? AND id!=? ORDER BY taken_at DESC LIMIT 1",
                        (meter_id, taken_at, skip)).fetchone()
    nxt = conn.execute("SELECT * FROM readings WHERE meter_id=? AND taken_at>? AND id!=? ORDER BY taken_at LIMIT 1",
                       (meter_id, taken_at, skip)).fetchone()
    for other, later in ((prev, False), (nxt, True)):
        if not other:
            continue
        for name, mine, theirs in (("Bezug (1.8.0)", imp, other["import_kwh"]),
                                   ("Einspeisung (2.8.0)", exp, other["export_kwh"])):
            if mine is None or theirs is None:
                continue
            if (later and mine > theirs) or (not later and mine < theirs):
                raise HTTPException(422, f"{name}: {mine:g} kWh passt nicht zur Ablesung vom "
                                         f"{_fmt(other['taken_at'])} ({theirs:g} kWh) - ein Zähler läuft nur vorwärts. "
                                         "Wurde der Zähler getauscht? Dann bitte 'Zählerwechsel' erfassen.")


def _insert_reading(conn, meter_id, taken_at, imp, exp, kind, note):
    try:
        conn.execute("INSERT INTO readings(meter_id,taken_at,import_kwh,export_kwh,kind,note,created_at)"
                     " VALUES(?,?,?,?,?,?,?)", (meter_id, taken_at, imp, exp, kind, note, sync._now_iso()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Für diesen Zeitpunkt gibt es bereits eine Ablesung.")


def _meter_dates(body: MeterIn) -> tuple[int | None, int | None]:
    installed = _when(body.installed_at) if body.installed_at else None
    removed = _when(body.removed_at) if body.removed_at else None
    if installed and removed and removed < installed:
        raise HTTPException(422, "Der Ausbau liegt vor dem Einbau.")
    return installed, removed


@app.get("/api/comparison", dependencies=[api])
def comparison():
    with db.connect() as conn:
        return analysis.comparison(conn, analysis.Dataset(conn))


@app.post("/api/meters", dependencies=[api])
def create_meter(body: MeterIn):
    installed, removed = _meter_dates(body)
    if body.start_import_kwh is not None and installed is None:
        raise HTTPException(422, "Für einen Anfangsstand wird das Einbaudatum benötigt.")
    with db.connect() as conn:
        meter_id = conn.execute("INSERT INTO meters(number,label,installed_at,removed_at,note) VALUES(?,?,?,?,?)",
                                (body.number.strip(), body.label, installed, removed, body.note)).lastrowid
        if body.start_import_kwh is not None:
            _insert_reading(conn, meter_id, installed, body.start_import_kwh, body.start_export_kwh,
                            "install", "Anfangsstand")
    return {"id": meter_id}


@app.put("/api/meters/{meter_id}", dependencies=[api])
def update_meter(meter_id: int, body: MeterIn):
    installed, removed = _meter_dates(body)
    with db.connect() as conn:
        first, last = conn.execute("SELECT MIN(taken_at), MAX(taken_at) FROM readings WHERE meter_id=?",
                                   (meter_id,)).fetchone()
        if installed and first and first < installed:
            raise HTTPException(422, "Es gibt Ablesungen vor diesem Einbaudatum.")
        if removed and last and last > removed:
            raise HTTPException(422, f"Es gibt Ablesungen nach diesem Ausbaudatum (zuletzt {_fmt(last)}).")
        done = conn.execute("UPDATE meters SET number=?, label=?, installed_at=?, removed_at=?, note=? WHERE id=?",
                            (body.number.strip(), body.label, installed, removed, body.note, meter_id)).rowcount
    if not done:
        raise HTTPException(404, "Zähler nicht gefunden.")
    return {"ok": True}


@app.delete("/api/meters/{meter_id}", dependencies=[api])
def delete_meter(meter_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM meters WHERE id=?", (meter_id,))
    return {"ok": True}


@app.post("/api/readings", dependencies=[api])
def create_reading(body: ReadingIn):
    taken = _when(body.taken_at)
    with db.connect() as conn:
        _check_reading(conn, body.meter_id, taken, body.import_kwh, body.export_kwh)
        _insert_reading(conn, body.meter_id, taken, body.import_kwh, body.export_kwh, "reading", body.note)
    return {"ok": True}


@app.put("/api/readings/{reading_id}", dependencies=[api])
def update_reading(reading_id: int, body: ReadingIn):
    taken = _when(body.taken_at)
    with db.connect() as conn:
        old = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Ablesung nicht gefunden.")
        _check_reading(conn, old["meter_id"], taken, body.import_kwh, body.export_kwh, skip_id=reading_id)
        try:
            conn.execute("UPDATE readings SET taken_at=?, import_kwh=?, export_kwh=?, note=? WHERE id=?",
                         (taken, body.import_kwh, body.export_kwh, body.note, reading_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Für diesen Zeitpunkt gibt es bereits eine Ablesung.")
        # Keep the meter's install/removal instants in step with their marker readings.
        if old["kind"] == "install":
            conn.execute("UPDATE meters SET installed_at=? WHERE id=?", (taken, old["meter_id"]))
        elif old["kind"] == "removal":
            conn.execute("UPDATE meters SET removed_at=? WHERE id=?", (taken, old["meter_id"]))
    return {"ok": True}


@app.delete("/api/readings/{reading_id}", dependencies=[api])
def delete_reading(reading_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM readings WHERE id=?", (reading_id,))
    return {"ok": True}


@app.post("/api/meter-change", dependencies=[api])
def meter_change(body: MeterChange):
    """Old meter out, new meter in: closes the old one with its final value and opens the new one."""
    at = _when(body.changed_at)
    if body.old_final_unknown:
        body.old_final_import_kwh = body.old_final_export_kwh = None
    elif body.old_final_import_kwh is None or body.old_final_export_kwh is None:
        raise HTTPException(422, "Bitte beide Endstände des alten Zählers eintragen (1.8.0 und 2.8.0) "
                                 "oder 'Endstand unbekannt' ankreuzen.")
    with db.connect() as conn:
        old = conn.execute("SELECT * FROM meters WHERE id=?", (body.old_meter_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Alter Zähler nicht gefunden.")
        if old["removed_at"]:
            raise HTTPException(422, f"Zähler {old['number']} ist bereits als ausgebaut erfasst.")
        last = conn.execute("SELECT MAX(taken_at) FROM readings WHERE meter_id=?", (old["id"],)).fetchone()[0]
        if last and last > at:
            raise HTTPException(422, f"Für Zähler {old['number']} gibt es Ablesungen nach diesem Zeitpunkt "
                                     f"(zuletzt {_fmt(last)}).")
        if body.old_final_import_kwh is not None:
            _check_reading(conn, old["id"], at, body.old_final_import_kwh, body.old_final_export_kwh)
            if last == at:
                conn.execute("UPDATE readings SET import_kwh=?, export_kwh=?, kind='removal' WHERE meter_id=? AND taken_at=?",
                             (body.old_final_import_kwh, body.old_final_export_kwh, old["id"], at))
            else:
                _insert_reading(conn, old["id"], at, body.old_final_import_kwh, body.old_final_export_kwh,
                                "removal", "Endstand beim Zählerwechsel")
        conn.execute("UPDATE meters SET removed_at=? WHERE id=?", (at, old["id"]))
        new_id = conn.execute("INSERT INTO meters(number,label,installed_at,note) VALUES(?,?,?,?)",
                              (body.new_number.strip(), old["label"], at, body.note)).lastrowid
        _insert_reading(conn, new_id, at, body.new_start_import_kwh, body.new_start_export_kwh,
                        "install", "Anfangsstand beim Zählerwechsel")
    return {"id": new_id}


# ---- electricity contract and cost -----------------------------------------------------

class TariffIn(BaseModel):
    base_eur_month: float = Field(ge=0, le=500)
    price_ct_kwh: float = Field(gt=0, le=300)
    valid_from: date | None = None


@app.get("/api/costs", dependencies=[api])
def get_costs():
    with db.connect() as conn:
        return analysis.costs(conn, analysis.Dataset(conn))


@app.post("/api/tariffs", dependencies=[api])
def save_tariff(body: TariffIn):
    """Create the price valid from a date, or replace it if that date already has one."""
    valid_from = body.valid_from.isoformat() if body.valid_from else ""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tariffs(valid_from, base_eur_month, price_ct_kwh) VALUES(?,?,?)"
            " ON CONFLICT(valid_from) DO UPDATE SET base_eur_month=excluded.base_eur_month,"
            " price_ct_kwh=excluded.price_ct_kwh", (valid_from, body.base_eur_month, body.price_ct_kwh))
    return {"ok": True}


@app.delete("/api/tariffs/{tariff_id}", dependencies=[api])
def delete_tariff(tariff_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM tariffs WHERE id=?", (tariff_id,))
    return {"ok": True}


class UIFiles(StaticFiles):
    """The UI has no versioned file names, so browsers must ask before reusing a stored copy.

    'no-cache' still lets them keep the copy: the server answers 304 when nothing changed.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", UIFiles(directory=STATIC, html=True), name="static")
