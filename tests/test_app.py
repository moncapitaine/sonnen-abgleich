"""Tests for import, coverage/gap logic and the meter comparison. Run: python -m pytest"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import analysis, config, db, ingest

STAT_HEAD = "timestamp,produced_energy,consumed_energy,battery_charged_energy,battery_discharged_energy,grid_feedin_energy,grid_purchase_energy"
MEAS_HEAD = "timestamp,production,consumption,battery_charge,battery_discharge,grid_feedin,grid_consumption,battery_state_of_charge,direct_consumption"


def stamp(dt: datetime) -> str:
    return dt.replace(tzinfo=config.TZ).isoformat()


def make_csvs(start: date, days: int, skip=(), zero=()):
    """Every day: 24 kWh consumed, 12 kWh from the grid, spread evenly (1000 W load, 500 W grid)."""
    stats, meas = [STAT_HEAD], [MEAS_HEAD]
    for i in range(days):
        d = start + timedelta(days=i)
        if d in skip:
            continue
        z = d in zero
        midnight = datetime(d.year, d.month, d.day)
        stats.append(f"{stamp(midnight)}," + ("0,0,0,0,0,0" if z else "6000,24000,2000,1800,1000,12000"))
        for hour in range(0, 24, 3):
            meas.append(f"{stamp(midnight + timedelta(hours=hour))}," + ("0,0,0,0,0,0,0,0" if z else "250,1000,83,75,42,500,50,125"))
    return "\n".join(stats), "\n".join(meas)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite")
    monkeypatch.setattr(config, "IMPORT_DIR", tmp_path / "import")
    monkeypatch.setattr(config, "SYNC_ENABLED", False)
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    from app.main import app
    with TestClient(app) as c:
        yield c


def load(stats, meas):
    with db.connect() as conn:
        ingest.import_csv(conn, stats)
        ingest.import_csv(conn, meas)


def test_import_is_idempotent_and_zero_rows_do_not_erase(client):
    stats, meas = make_csvs(date(2025, 6, 1), 10)
    load(stats, meas)
    load(stats, meas)
    zeros, _ = make_csvs(date(2025, 6, 1), 10, zero={date(2025, 6, 5)})
    with db.connect() as conn:
        ingest.import_csv(conn, zeros)
        assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM power").fetchone()[0] == 80
        assert conn.execute("SELECT purchase FROM daily WHERE day='2025-06-05'").fetchone()[0] == 12000


def test_hourly_rows_replace_three_hour_rows(client):
    _, meas = make_csvs(date(2025, 6, 1), 2)
    hourly = [MEAS_HEAD] + [f"{stamp(datetime(2025, 6, 1) + timedelta(hours=h))},250,1000,83,75,42,500,50,125" for h in range(24)]
    with db.connect() as conn:
        ingest.import_csv(conn, meas)
        ingest.import_csv(conn, "\n".join(hourly))
        ingest.import_csv(conn, meas)  # coarser data must not come back
        rows = conn.execute("SELECT dur, COUNT(*) FROM power GROUP BY dur ORDER BY dur").fetchall()
        assert [tuple(r) for r in rows] == [(3600, 24), (10800, 8)]
        ds = analysis.Dataset(conn)
    t1, t2 = analysis.day_bounds(date(2025, 6, 1))
    assert ds.between(t1, t2)["purchase"] == 12000


def test_gap_detection_and_estimate(client):
    start = date(2025, 1, 1)
    missing = {start + timedelta(days=i) for i in range(400, 410)}   # no rows at all
    zero = {start + timedelta(days=i) for i in range(410, 420)}      # rows full of zeros
    load(*make_csvs(start, 500, skip=missing, zero=zero))
    with db.connect() as conn:
        ds = analysis.Dataset(conn)
    gaps = ds.gaps()
    assert len(gaps) == 1
    g = gaps[0]
    assert g["from"] == (start + timedelta(days=400)).isoformat()
    assert g["to"] == (start + timedelta(days=419)).isoformat()
    assert g["missing_days"] == 20
    # the same days one year earlier: 12 kWh/day from the grid, 24 kWh/day consumed
    assert g["est_kwh"]["imp_lo"] == pytest.approx(240, abs=0.5)
    assert g["est_kwh"]["imp_hi"] == pytest.approx(480, abs=0.5)
    assert g["est_kwh"]["imp_lo"] < g["est_kwh"]["imp_mid"] < g["est_kwh"]["imp_hi"]


def test_between_uses_sub_daily_data_for_partial_days(client):
    load(*make_csvs(date(2025, 6, 1), 10))
    with db.connect() as conn:
        ds = analysis.Dataset(conn)
    t1 = analysis.parse_local("2025-06-02T06:00")
    t2 = analysis.parse_local("2025-06-04T18:00")   # 18 h + 1 full day + 18 h = 60 h at 500 W
    e = ds.between(t1, t2)
    assert e["purchase"] == pytest.approx(30000)
    assert e["missing_days"] == 0 and not e["approx"]
    assert ds.between(t2, t1)["purchase"] == 0


def _reading(client, meter_id, when, imp, exp=None):
    return client.post("/api/readings", json={"meter_id": meter_id, "taken_at": when, "import_kwh": imp, "export_kwh": exp})


def test_meter_comparison_and_meter_change(client):
    load(*make_csvs(date(2025, 6, 1), 60))
    mid = client.post("/api/meters", json={"number": "A-1", "installed_at": "2025-06-01T00:00", "start_import_kwh": 1000}).json()["id"]
    assert _reading(client, mid, "2025-06-11T00:00", 1120).status_code == 200      # 10 days * 12 kWh = sonnen
    assert _reading(client, mid, "2025-06-21T00:00", 1300).status_code == 200      # meter counts 180, sonnen 120

    # a meter never runs backwards, and readings must sit between their neighbours
    r = _reading(client, mid, "2025-06-25T00:00", 1200)
    assert r.status_code == 422 and "vorwärts" in r.json()["detail"]
    assert _reading(client, mid, "2025-06-15T00:00", 1400).status_code == 422
    assert _reading(client, mid, "2025-06-21T00:00", 1300).status_code == 409      # duplicate instant

    # swap: old meter ends at 1360 (=+60 over 5 days, matches), new one starts at 0
    r = client.post("/api/meter-change", json={"old_meter_id": mid, "changed_at": "2025-06-26T00:00",
                                               "old_final_import_kwh": 1360, "old_final_export_kwh": 0,
                                               "new_number": "B-2", "new_start_import_kwh": 0, "new_start_export_kwh": 0})
    new_id = r.json()["id"]
    assert _reading(client, mid, "2025-07-01T00:00", 1400).status_code == 422      # old meter is gone
    assert _reading(client, new_id, "2025-07-06T00:00", 120).status_code == 200    # low number is fine on the new meter

    cmp = client.get("/api/comparison").json()
    got = [(i["meter_number"], i["meter_import"], i["sonnen_import"], i["verdict"]) for i in cmp["intervals"]]
    assert got == [("A-1", 120, 120, "ok"), ("A-1", 180, 120, "high"), ("A-1", 60, 60, "ok"), ("B-2", 120, 120, "ok")]
    assert cmp["breaks"] == []
    assert cmp["total"]["meter_import"] == 480 and cmp["total"]["sonnen_import"] == 420
    new = [m for m in cmp["meters"] if m["number"] == "B-2"][0]
    assert new["expected"]["import_kwh"] == pytest.approx(120 + 25 * 12)          # 6 July -> 31 July 00:00, where the data ends


def test_comparison_flags_gap_as_explanation(client):
    start = date(2025, 1, 1)
    zero = {start + timedelta(days=i) for i in range(400, 420)}
    load(*make_csvs(start, 450, zero=zero))
    mid = client.post("/api/meters", json={"number": "A-1"}).json()["id"]
    d0, d1 = start + timedelta(days=390), start + timedelta(days=430)
    _reading(client, mid, f"{d0}T00:00", 5000)
    _reading(client, mid, f"{d1}T00:00", 5000 + 20 * 12 + 20 * 24)   # recorded days + whole consumption in the gap
    i = client.get("/api/comparison").json()["intervals"][0]
    assert i["sonnen_import"] == 240 and i["missing_days"] == 20
    assert i["est_import"][0] == pytest.approx(240, abs=1) and i["est_import"][2] == pytest.approx(480, abs=1)
    assert i["verdict"] == "gap"


def test_login_required_when_password_set(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "geheim")
    assert client.get("/api/daily").status_code == 401
    assert client.post("/api/login", json={"password": "geheim"}).status_code == 200
    assert client.get("/api/daily").status_code == 200


def test_export_register_is_compared_too(client):
    """2.8.0 (feed-in) gets the same treatment as 1.8.0: expected value, deviation and verdict."""
    load(*make_csvs(date(2025, 6, 1), 30))            # 1 kWh/day feed-in, 12 kWh/day purchase
    mid = client.post("/api/meters", json={"number": "A-1", "installed_at": "2025-06-01T00:00",
                                           "start_import_kwh": 100, "start_export_kwh": 500}).json()["id"]
    _reading(client, mid, "2025-06-11T00:00", 220, 510)    # both as sonnen recorded
    r = _reading(client, mid, "2025-06-21T00:00", 340, 505)
    assert r.status_code == 422 and "Einspeisung" in r.json()["detail"]   # 2.8.0 cannot run backwards either
    _reading(client, mid, "2025-06-21T00:00", 340, 560)    # meter fed in 50, sonnen says 10
    cmp = client.get("/api/comparison").json()
    a, b = cmp["intervals"]
    assert (a["meter_export"], a["sonnen_export"], a["diff_export"], a["verdict_export"]) == (10, 10, 0, "ok")
    assert (b["expected_import"], b["expected_export"]) == (340, 520)
    assert (b["diff_export"], b["verdict_export"], b["verdict"]) == (40, "high", "ok")
    assert cmp["total"]["export"]["meter"] == 60 and cmp["total"]["export"]["diff"] == 40


def test_meter_change_needs_all_four_values_or_an_explicit_unknown(client):
    load(*make_csvs(date(2025, 6, 1), 30))
    mid = client.post("/api/meters", json={"number": "A-1"}).json()["id"]
    _reading(client, mid, "2025-06-05T00:00", 100, 10)
    swap = {"old_meter_id": mid, "changed_at": "2025-06-10T12:00", "new_number": "B-2",
            "new_start_import_kwh": 0, "new_start_export_kwh": 0}
    assert client.post("/api/meter-change", json={**swap, "old_final_import_kwh": 160}).status_code == 422   # 2.8.0 missing
    assert client.post("/api/meter-change", json={k: v for k, v in swap.items() if k != "new_start_export_kwh"}).status_code == 422
    assert client.post("/api/meter-change", json={**swap, "old_final_unknown": True}).status_code == 200
    new_id = [m["id"] for m in client.get("/api/comparison").json()["meters"] if m["number"] == "B-2"][0]
    _reading(client, new_id, "2025-06-20T12:00", 120, 10)
    cmp = client.get("/api/comparison").json()
    # only the new meter can be compared; the stretch up to the swap is reported as not comparable
    assert [(i["meter_number"], i["meter_import"], i["sonnen_import"]) for i in cmp["intervals"]] == [("B-2", 120, 120)]
    assert len(cmp["breaks"]) == 1 and cmp["breaks"][0]["old_meter"] == "A-1"


def test_build_info_and_no_stale_ui(client):
    health = client.get("/api/health").json()
    assert health["ok"] and health["revision"] and "built_at" in health and health["started_at"]
    assert client.get("/api/overview").json()["build"]["revision"] == health["revision"]
    for path in ("/", "/app.js", "/app.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    # what a phone needs to put the app on its home screen
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/apple-touch-icon.png").status_code == 200


def test_costs_and_advance_payment_proposal(client):
    start = date(2025, 1, 1)
    zero = {start + timedelta(days=i) for i in range(400, 420)}       # 20 days without sonnen data
    load(*make_csvs(start, 500, zero=zero))                           # 12 kWh/day from the grid otherwise
    assert client.get("/api/costs").json()["proposal"] is None        # no contract entered yet
    assert client.post("/api/tariffs", json={"base_eur_month": 10, "price_ct_kwh": 30}).status_code == 200
    assert client.post("/api/tariffs", json={"base_eur_month": 10, "price_ct_kwh": 0}).status_code == 422
    c = client.get("/api/costs").json()

    june = [m for m in c["months"] if m["month"] == "2025-06"][0]     # 30 days * 12 kWh = 360 kWh
    assert (june["kwh"], june["energy_eur"], june["base_eur"], june["total_eur"], june["complete"]) == (360, 108, 10, 118, True)
    assert [y for y in c["years"] if y["year"] == "2025"][0]["total_eur"] == pytest.approx(365 * 12 * 0.30 + 120)

    # the gap is filled with what the same days needed a year earlier, so the year comes out at 365 * 12 kWh
    p = c["proposal"]
    assert p["days"] == 365 and p["missing_days"] == 20 and not p["extrapolated"]
    assert p["recorded_kwh"] == 345 * 12 and p["filled_kwh"] == pytest.approx(240, abs=0.5)
    assert p["annual_kwh"] == pytest.approx(4380, abs=0.5) and p["annual_kwh_rounded"] == 4400
    assert p["monthly_payment"] == 120                                # (12 * 10 + 4400 * 0.30) / 12

    # a price change applies from its date on; earlier months keep the old price
    client.post("/api/tariffs", json={"base_eur_month": 12, "price_ct_kwh": 40, "valid_from": "2026-01-01"})
    c = client.get("/api/costs").json()
    assert [m for m in c["months"] if m["month"] == "2025-06"][0]["total_eur"] == 118
    assert [m for m in c["months"] if m["month"] == "2026-01"][0]["total_eur"] == pytest.approx(31 * 12 * 0.40 + 12)
    assert c["proposal"]["monthly_payment"] == 159                    # (12 * 12 + 4400 * 0.40) / 12 = 158.67
    # saving again for the same date replaces instead of adding
    client.post("/api/tariffs", json={"base_eur_month": 12, "price_ct_kwh": 35, "valid_from": "2026-01-01"})
    assert len(client.get("/api/costs").json()["tariffs"]) == 2
