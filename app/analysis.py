"""Analysis on top of the stored sonnen data.

The central question is "how much energy did sonnen record between two points in
time, and how much of that time has no sonnen data at all?" - that is what makes a
comparison with the official meter meaningful.
"""
import calendar
import math
from datetime import date, datetime, timedelta
from statistics import median

from . import config

FIELDS = ["produced", "consumed", "bat_charged", "bat_discharged", "feedin", "purchase"]
FULL = 0.999  # coverage above this counts as a complete day


def day_bounds(d: date) -> tuple[int, int]:
    start = datetime(d.year, d.month, d.day, tzinfo=config.TZ)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=config.TZ)
    return int(start.timestamp()), int(end.timestamp())


def local_date(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, config.TZ).date()


def local_iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%Y-%m-%dT%H:%M")


def parse_local(value: str) -> int:
    """'2026-11-17T08:30' (house local time) or any ISO string with offset -> UTC epoch."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return int(dt.timestamp())


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


class Dataset:
    """All sonnen data in memory (a few thousand rows) plus derived coverage."""

    def __init__(self, conn):
        self.days: dict[date, dict] = {}
        for r in conn.execute("SELECT * FROM daily ORDER BY day"):
            self.days[date.fromisoformat(r["day"])] = {f: r[f] for f in FIELDS}

        # Sub-daily segments per local day: (start, dur, {field: Wh}, has_data)
        energy: dict[date, list] = {}
        for r in conn.execute("SELECT * FROM energy_fine ORDER BY ts"):
            vals = {f: r[f] for f in FIELDS}
            energy.setdefault(local_date(r["ts"]), []).append((r["ts"], r["dur"], vals, self._has(vals)))
        power: dict[date, list] = {}
        self.soc: dict[date, list] = {}
        for r in conn.execute("SELECT * FROM power ORDER BY ts"):
            h = r["dur"] / 3600
            vals = {"produced": r["production"] * h, "consumed": r["consumption"] * h,
                    "bat_charged": r["bat_charge"] * h, "bat_discharged": r["bat_discharge"] * h,
                    "feedin": r["feedin"] * h, "purchase": r["purchase"] * h}
            power.setdefault(local_date(r["ts"]), []).append((r["ts"], r["dur"], vals, self._has(vals)))
            if r["soc"] is not None:
                self.soc.setdefault(local_date(r["ts"]), []).append(r["soc"])
        self.fine: dict[date, list] = {}
        for d in set(energy) | set(power):
            e, p = energy.get(d, []), power.get(d, [])
            covered = lambda segs: sum(s[1] for s in segs if s[3])
            self.fine[d] = e if e and covered(e) >= covered(p) else p

        with_data = [d for d, v in self.days.items() if self._has(v)]
        with_data += [d for d, segs in self.fine.items() if any(s[3] for s in segs)]
        self.first_day = min(with_data) if with_data else None
        self.last_day = max(with_data) if with_data else None
        self.data_end = None  # epoch up to which sonnen data exists
        if self.last_day:
            segs = [s for s in self.fine.get(self.last_day, []) if s[3]]
            self.data_end = max(s[0] + s[1] for s in segs) if segs else day_bounds(self.last_day)[1]

        self.cov: dict[date, float] = {}
        for d in self.all_days():
            self.cov[d] = self._coverage(d)
        self._ref_cache: dict[date, tuple] = {}

    @staticmethod
    def _has(vals) -> bool:
        # A house never draws exactly 0 W for hours; all-zero means "nothing was recorded".
        return bool(vals["consumed"] or vals["produced"] or vals["purchase"])

    def all_days(self):
        if not self.first_day:
            return []
        n = (self.last_day - self.first_day).days
        return [self.first_day + timedelta(days=i) for i in range(n + 1)]

    def _coverage(self, d: date) -> float:
        row = self.days.get(d)
        segs = self.fine.get(d)
        if not segs:
            return 1.0 if row and self._has(row) else 0.0
        start, end = day_bounds(d)
        good = [s for s in segs if s[3]]
        if d == self.first_day and good:   # recording started during this day
            start = min(s[0] for s in good)
        if d == self.last_day and good:    # the rest of today has simply not happened/synced yet
            end = max(s[0] + s[1] for s in good)
        covered = sum(max(0, min(s[0] + s[1], end) - max(s[0], start)) for s in good)
        cov = min(1.0, covered / max(1, end - start))
        return 1.0 if cov > FULL else cov

    # ---- reference values for days without data --------------------------------

    def _ref(self, d: date) -> tuple[dict, str]:
        """Typical daily values (Wh) for date d, taken from the same week in another year."""
        if d in self._ref_cache:
            return self._ref_cache[d]
        chosen, label = [], ""
        for shift in (-1, 1, -2, 2):
            base = _shift_year(d, shift)
            cands = [base + timedelta(days=k) for k in range(-3, 4)]
            good = [self.days[x] for x in cands if x in self.days and self.cov.get(x, 0) >= FULL]
            if len(good) >= 4:
                chosen, label = good, f"gleiche Woche {base.year}"
                break
        if not chosen:
            good_days = sorted((x for x in self.days if self.cov.get(x, 0) >= FULL),
                               key=lambda x: abs((x - d).days))[:28]
            chosen, label = [self.days[x] for x in good_days], "nächstgelegene Tage mit Daten"
        ref = {f: (sum(c[f] for c in chosen) / len(chosen) if chosen else 0.0) for f in FIELDS}
        ref["direct"] = max(0.0, ref["produced"] - ref["feedin"] - ref["bat_charged"])
        self._ref_cache[d] = (ref, label)
        return self._ref_cache[d]

    @staticmethod
    def _estimate(ref: dict, frac: float) -> dict:
        """Grid energy that plausibly went unrecorded for `frac` of a day, three scenarios."""
        return {
            # system ran normally, only the recording is missing
            "imp_lo": frac * ref["purchase"],
            # PV ran, battery was out of service: everything not covered directly by PV came from the grid
            "imp_mid": frac * max(0.0, ref["consumed"] - ref["direct"]),
            # PV and battery both off: the whole consumption came from the grid
            "imp_hi": frac * ref["consumed"],
            "exp_lo": 0.0,
            "exp_hi": frac * (ref["feedin"] + ref["bat_charged"]),
        }

    # ---- energy between two instants -------------------------------------------

    def between(self, t1: int, t2: int) -> dict:
        res = {f: 0.0 for f in FIELDS}
        res.update(missing_days=0.0, pending_days=0.0, approx=False,
                   imp_lo=0.0, imp_mid=0.0, imp_hi=0.0, exp_lo=0.0, exp_hi=0.0, ref_labels=[])
        if t2 <= t1:
            return res
        d, last = local_date(t1), local_date(t2 - 1)
        while d <= last:
            ds, de = day_bounds(d)
            a, b = max(t1, ds), min(t2, de)
            if b > a:
                self._add_day(res, d, ds, de, a, b)
            d += timedelta(days=1)
        res["ref_labels"] = sorted(set(res["ref_labels"]))
        return res

    def _add_day(self, res, d, ds, de, a, b):
        length = de - ds
        frac = (b - a) / length
        row = self.days.get(d)
        segs = self.fine.get(d)
        full = a == ds and b == de
        if full and (not segs or (row and self._has(row))):
            if row:  # the daily total is the authoritative figure for a whole day
                for f in FIELDS:
                    res[f] += row[f]
            miss = 1.0 - self.cov.get(d, 0.0)
        elif segs:
            covered = 0
            for ts, dur, vals, has in segs:
                ov = min(ts + dur, b) - max(ts, a)
                if ov <= 0 or not has:
                    continue
                covered += ov
                for f in FIELDS:
                    res[f] += vals[f] * ov / dur
            miss = max(0, (b - a) - covered) / length
        elif row and self._has(row):
            for f in FIELDS:  # only a daily total exists: spread it evenly over the day
                res[f] += row[f] * frac
            res["approx"] = True
            miss = 0.0
        else:
            miss = frac
        if miss > 0.001:
            res["missing_days"] += miss
            if self.data_end is not None and a >= self.data_end or (self.last_day and d > self.last_day):
                res["pending_days"] += miss
            ref, label = self._ref(d)
            for k, v in self._estimate(ref, miss).items():
                res[k] += v
            res["ref_labels"].append(label)

    # ---- per-day output for the charts -------------------------------------------

    def daily_table(self) -> dict:
        days = self.all_days()
        cols = {k: [] for k in ["day", *FIELDS, "cov", "base_w", "soc_max", "x_consumed", "x_purchase"]}
        for d in days:
            row = self.days.get(d) or {f: 0 for f in FIELDS}
            cols["day"].append(d.isoformat())
            for f in FIELDS:
                cols[f].append(row[f])
            cols["cov"].append(round(self.cov[d], 3))
            cols["base_w"].append(self._base_load(d))
            cols["soc_max"].append(max(self.soc[d]) if d in self.soc else None)
        for field, key in (("consumed", "x_consumed"), ("purchase", "x_purchase")):
            cols[key] = self._anomalies(days, field)
        return cols

    def _base_load(self, d: date):
        """Average house load 00:00-06:00 in W - a device left running shows up here."""
        ds, _ = day_bounds(d)
        wh = secs = 0
        for ts, dur, vals, has in self.fine.get(d, []):
            if has and ts - ds < 6 * 3600:
                wh += vals["consumed"]
                secs += dur
        return round(wh / (secs / 3600)) if secs >= 3 * 3600 else None

    def _anomalies(self, days, field) -> list:
        """Excess over the local norm (Wh) for days that stick out, else 0.

        Robust z-score against the median of the surrounding four weeks.
        """
        out = []
        good = [(d, self.days[d][field]) for d in days if self.cov[d] >= FULL and d in self.days]
        index = {d: i for i, (d, _) in enumerate(good)}
        for d in days:
            i = index.get(d)
            if i is None:
                out.append(0)
                continue
            x = good[i][1]
            near = [v for dd, v in good[max(0, i - 14): i + 15] if dd != d and abs((dd - d).days) <= 14]
            if len(near) < 8:
                out.append(0)
                continue
            med = median(near)
            mad = max(median(abs(v - med) for v in near), 0.05 * med, 500)
            z = 0.6745 * (x - med) / mad
            out.append(round(x - med) if z > 3.5 and x - med > 5000 else 0)
        return out

    def gaps(self) -> list[dict]:
        """Periods without (complete) sonnen data, with an estimate of what went unrecorded."""
        out, cur = [], None
        for d in self.all_days():
            c = self.cov[d]
            if c < FULL:
                if cur is None:
                    cur = {"from": d, "to": d, "missing_days": 0.0, "est": {}, "labels": set()}
                    out.append(cur)
                cur["to"] = d
                cur["missing_days"] += 1 - c
                ref, label = self._ref(d)
                cur["labels"].add(label)
                for k, v in self._estimate(ref, 1 - c).items():
                    cur["est"][k] = cur["est"].get(k, 0.0) + v
            else:
                cur = None
        return [{
            "from": g["from"].isoformat(), "to": g["to"].isoformat(),
            "missing_days": round(g["missing_days"], 2),
            "est_kwh": {k: round(v / 1000, 1) for k, v in g["est"].items()},
            "reference": ", ".join(sorted(g["labels"])),
        } for g in out if g["missing_days"] >= 0.1]


# ---- official meter vs. sonnen ------------------------------------------------------

def _verdict(diff: float, meter: float, missing_days: float, lo: float, hi: float) -> str:
    """Judge meter minus sonnen (kWh). lo/hi: plausible unrecorded energy for the time without sonnen data."""
    tol = max(2.0, 0.03 * abs(meter))
    if missing_days > 0.02:
        if lo - tol - 0.1 * (hi - lo) <= diff <= hi + tol + 0.1 * (hi - lo):
            return "gap"        # explained by the period without sonnen data
        return "gap_unexplained"
    if abs(diff) <= tol:
        return "ok"
    return "high" if diff > 0 else "low"


def comparison(conn, ds: Dataset) -> dict:
    meters = [dict(r) for r in conn.execute("SELECT * FROM meters ORDER BY COALESCE(installed_at, 0), id")]
    intervals, breaks = [], []
    prev_last = None
    for m in meters:
        rs = [dict(r) for r in conn.execute(
            "SELECT * FROM readings WHERE meter_id=? ORDER BY taken_at", (m["id"],))]
        m["readings"] = [{**r, "taken_local": local_iso(r["taken_at"])} for r in rs]
        m["installed_local"], m["removed_local"] = local_iso(m["installed_at"]), local_iso(m["removed_at"])
        m["expected"] = None
        if rs and prev_last and rs[0]["taken_at"] - prev_last["taken_at"] > 3600:
            breaks.append({"from": local_iso(prev_last["taken_at"]), "to": local_iso(rs[0]["taken_at"]),
                           "old_meter": prev_last["number"], "new_meter": m["number"]})
        for r1, r2 in zip(rs, rs[1:]):
            e = ds.between(r1["taken_at"], r2["taken_at"])
            days = (r2["taken_at"] - r1["taken_at"]) / 86400
            meter_imp = r2["import_kwh"] - r1["import_kwh"]
            son_imp = e["purchase"] / 1000
            item = {
                "meter_id": m["id"], "meter_number": m["number"],
                "from": local_iso(r1["taken_at"]), "to": local_iso(r2["taken_at"]), "days": round(days, 2),
                "meter_import": round(meter_imp, 2), "sonnen_import": round(son_imp, 2),
                "diff_import": round(meter_imp - son_imp, 2),
                "diff_pct": round(100 * (meter_imp - son_imp) / meter_imp, 1) if meter_imp > 0 else None,
                "missing_days": round(e["missing_days"], 2), "pending_days": round(e["pending_days"], 2),
                "approx": e["approx"],
                "est_import": [round(e[k] / 1000, 1) for k in ("imp_lo", "imp_mid", "imp_hi")],
                "est_export": [round(e[k] / 1000, 1) for k in ("exp_lo", "exp_hi")],
                "reference": ", ".join(e["ref_labels"]),
                "verdict": _verdict(meter_imp - son_imp, meter_imp, e["missing_days"],
                                    e["imp_lo"] / 1000, e["imp_hi"] / 1000),
                # what the later reading should have shown, going by sonnen
                "reading_id": r2["id"], "expected_import": round(r1["import_kwh"] + son_imp, 1),
                "expected_export": None, "verdict_export": None,
                "meter_export": None, "sonnen_export": round(e["feedin"] / 1000, 2), "diff_export": None,
                "sonnen_consumed": round(e["consumed"] / 1000, 2), "sonnen_produced": round(e["produced"] / 1000, 2),
            }
            if r1["export_kwh"] is not None and r2["export_kwh"] is not None:
                item["meter_export"] = round(r2["export_kwh"] - r1["export_kwh"], 2)
                item["diff_export"] = round(item["meter_export"] - item["sonnen_export"], 2)
                item["expected_export"] = round(r1["export_kwh"] + item["sonnen_export"], 1)
                item["verdict_export"] = _verdict(item["diff_export"], item["meter_export"], e["missing_days"],
                                                  e["exp_lo"] / 1000, e["exp_hi"] / 1000)
            intervals.append(item)
        m["final_estimate"] = None
        if rs and m["removed_at"] and m["removed_at"] - rs[-1]["taken_at"] > 3600:
            # Removed without a final reading: what it should have shown when it came out, going by sonnen.
            e = ds.between(rs[-1]["taken_at"], m["removed_at"])
            m["final_estimate"] = {
                "at": local_iso(m["removed_at"]), "since": local_iso(rs[-1]["taken_at"]),
                "import_kwh": round(rs[-1]["import_kwh"] + e["purchase"] / 1000, 1),
                "export_kwh": (round(rs[-1]["export_kwh"] + e["feedin"] / 1000, 1)
                               if rs[-1]["export_kwh"] is not None else None),
                "missing_days": round(e["missing_days"], 1),
                "est_import": [round(e[k] / 1000, 1) for k in ("imp_lo", "imp_mid", "imp_hi")],
            }
        if rs:
            prev_last = {**rs[-1], "number": m["number"]}
            if m["removed_at"] is None and ds.data_end and ds.data_end > rs[-1]["taken_at"]:
                e = ds.between(rs[-1]["taken_at"], ds.data_end)
                m["expected"] = {
                    "as_of": local_iso(ds.data_end),
                    "import_kwh": round(rs[-1]["import_kwh"] + e["purchase"] / 1000, 1),
                    "export_kwh": (round(rs[-1]["export_kwh"] + e["feedin"] / 1000, 1)
                                   if rs[-1]["export_kwh"] is not None else None),
                    "missing_days": round(e["missing_days"], 2),
                    "est_import": [round(e[k] / 1000, 1) for k in ("imp_lo", "imp_mid", "imp_hi")],
                }
    total = None
    if intervals:
        total = {
            "days": round(sum(i["days"] for i in intervals), 1),
            "meter_import": round(sum(i["meter_import"] for i in intervals), 1),
            "sonnen_import": round(sum(i["sonnen_import"] for i in intervals), 1),
            "missing_days": round(sum(i["missing_days"] for i in intervals), 1),
            "est_import": [round(sum(i["est_import"][k] for i in intervals), 1) for k in range(3)],
        }
        total["diff_import"] = round(total["meter_import"] - total["sonnen_import"], 1)
        ex = [i for i in intervals if i["meter_export"] is not None]
        total["export"] = {
            "days": round(sum(i["days"] for i in ex), 1),
            "meter": round(sum(i["meter_export"] for i in ex), 1),
            "sonnen": round(sum(i["sonnen_export"] for i in ex), 1),
            "missing_days": round(sum(i["missing_days"] for i in ex), 1),
            "est": [0.0, round(sum(i["est_export"][1] for i in ex), 1)],
        } if ex else None
        if total["export"]:
            total["export"]["diff"] = round(total["export"]["meter"] - total["export"]["sonnen"], 1)
    return {"meters": meters, "intervals": intervals, "breaks": breaks, "total": total}


# ---- cost of the grid purchase -------------------------------------------------------

def _billed_days(conn, ds: Dataset) -> tuple[dict, dict | None]:
    """Grid purchase per local day as the official meter counted it, in kWh.

    The provider bills by the meter, not by sonnen. Between two readings of a meter the
    counted energy is known exactly; it is spread over the days in between in proportion
    to what sonnen recorded (plus the estimate for time without sonnen data), so months
    and years get their fair share. Days no reading covers are estimated from sonnen,
    scaled by how much more the meter counts than sonnen where both are complete.

    Returns ({day: {"known", "est", "known_frac"}}, factor info or None).
    """
    intervals = []
    for m in conn.execute("SELECT id FROM meters ORDER BY id").fetchall():
        rs = conn.execute("SELECT taken_at, import_kwh FROM readings WHERE meter_id=? ORDER BY taken_at",
                          (m["id"],)).fetchall()
        intervals += [(a["taken_at"], b["taken_at"], b["import_kwh"] - a["import_kwh"])
                      for a, b in zip(rs, rs[1:]) if b["taken_at"] > a["taken_at"]]

    # How much more (or less) the meter counts than sonnen, from periods sonnen recorded completely.
    meter_sum = sonnen_sum = span = 0.0
    for t1, t2, delta in intervals:
        e = ds.between(t1, t2)
        if e["missing_days"] <= 0.02 and e["purchase"] > 0:
            meter_sum += delta
            sonnen_sum += e["purchase"] / 1000
            span += (t2 - t1) / 86400
    factor = None
    if span >= 60 and sonnen_sum >= 100 and 0.5 < meter_sum / sonnen_sum < 2:
        factor = {"value": meter_sum / sonnen_sum, "days": round(span, 1),
                  "meter_kwh": round(meter_sum, 1), "sonnen_kwh": round(sonnen_sum, 1)}
    scale = factor["value"] if factor else 1.0

    def weight(a: int, b: int) -> float:   # expected grid purchase of a stretch of time, kWh
        e = ds.between(a, b)
        return (e["purchase"] + e["imp_mid"]) / 1000

    known: dict[date, float] = {}
    known_w: dict[date, float] = {}
    known_secs: dict[date, int] = {}
    for t1, t2, delta in intervals:
        parts, d, last = [], local_date(t1), local_date(t2 - 1)
        while d <= last:
            start, end = day_bounds(d)
            a, b = max(t1, start), min(t2, end)
            if b > a:
                parts.append((d, b - a, weight(a, b)))
            d += timedelta(days=1)
        total = sum(w for _, _, w in parts)
        for d, secs, w in parts:
            share = w / total if total > 0 else secs / (t2 - t1)
            known[d] = known.get(d, 0.0) + delta * share
            known_w[d] = known_w.get(d, 0.0) + w
            known_secs[d] = known_secs.get(d, 0) + secs

    days: dict[date, dict] = {}
    if not ds.first_day:
        return days, factor
    d = min([ds.first_day] + [local_date(t1) for t1, _, _ in intervals])
    while d <= ds.last_day:
        start, end = day_bounds(d)
        if d == ds.last_day and ds.data_end:
            end = min(end, ds.data_end)
        length = max(1, end - start)
        covered = min(length, known_secs.get(d, 0))
        est = 0.0
        if d >= ds.first_day and covered < length - 60:
            est = max(0.0, weight(start, end) - known_w.get(d, 0.0)) * scale
        days[d] = {"known": known.get(d, 0.0), "est": est, "known_frac": covered / length}
        d += timedelta(days=1)
    return days, factor


def costs(conn, ds: Dataset) -> dict:
    """Monthly cost of the grid purchase and a proposal for the monthly advance payment.

    A German contract has a fixed base price per month and one price per kWh that does not
    vary over the year. The provider asks for the expected yearly consumption and derives
    twelve equal advance payments ("Abschlag") from it.
    """
    tariffs = [dict(r) for r in conn.execute("SELECT * FROM tariffs ORDER BY valid_from")]
    out = {"tariffs": tariffs, "months": [], "years": [], "proposal": None, "factor": None}
    if not tariffs or not ds.first_day:
        return out

    def tariff_for(d: date) -> dict:
        chosen = tariffs[0]   # days before the first known price are costed with that first price
        for t in tariffs:
            if t["valid_from"] <= d.isoformat():
                chosen = t
        return chosen

    billed, factor = _billed_days(conn, ds)
    out["factor"] = factor and {**factor, "value": round(factor["value"], 4)}

    def blank(key: str, name: str) -> dict:
        return {name: key, "days": 0, "kwh": 0.0, "known_kwh": 0.0, "est_kwh": 0.0, "known_days": 0.0,
                "energy_eur": 0.0, "base_eur": 0.0, "sonnen_kwh": 0.0, "sonnen_eur": 0.0,
                "sonnen_days": 0.0, "sonnen_missing_days": 0.0, "kwh_sonnen_days": 0.0}

    months: dict[str, dict] = {}
    for d, b in billed.items():
        t = tariff_for(d)
        price = t["price_ct_kwh"] / 100
        base = t["base_eur_month"] / calendar.monthrange(d.year, d.month)[1]
        m = months.setdefault(d.isoformat()[:7], blank(d.isoformat()[:7], "month"))
        kwh = b["known"] + b["est"]
        m["days"] += 1
        m["kwh"] += kwh
        m["known_kwh"] += b["known"]
        m["est_kwh"] += b["est"]
        m["known_days"] += b["known_frac"]
        m["energy_eur"] += kwh * price
        m["base_eur"] += base
        if d >= ds.first_day:      # what sonnen alone would say
            son = ds.days[d]["purchase"] / 1000 if d in ds.days else 0.0
            m["sonnen_kwh"] += son
            m["sonnen_eur"] += son * price + base
            m["sonnen_days"] += 1
            m["sonnen_missing_days"] += 1 - ds.cov[d]
            m["kwh_sonnen_days"] += kwh    # meter-side energy on the days sonnen can be compared with

    years: dict[str, dict] = {}
    sums = ("days", "kwh", "known_kwh", "est_kwh", "known_days", "energy_eur", "base_eur",
            "sonnen_kwh", "sonnen_eur", "sonnen_days", "sonnen_missing_days", "kwh_sonnen_days")
    for m in months.values():
        y, mo = int(m["month"][:4]), int(m["month"][5:])
        m["complete"] = m["days"] == calendar.monthrange(y, mo)[1]
        m["total_eur"] = m["energy_eur"] + m["base_eur"]
        yr = years.setdefault(str(y), {**blank(str(y), "year"), "months": 0.0})
        yr["months"] += m["days"] / calendar.monthrange(y, mo)[1]
        for k in sums:
            yr[k] += m[k]
    for yr in years.values():
        yr["total_eur"] = yr["energy_eur"] + yr["base_eur"]

    def tidy(item: dict) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in item.items()}
    out["months"] = [tidy(m) for m in months.values()]
    out["years"] = [tidy(y) for y in years.values()]

    # Proposal from the last 365 complete days.
    end = ds.last_day - timedelta(days=1)
    start = max(ds.first_day, end - timedelta(days=364))
    if end >= start:
        recorded = filled = missing = actual = actual_known_days = 0.0
        labels = set()
        n = (end - start).days + 1
        for i in range(n):
            d = start + timedelta(days=i)
            recorded += ds.days[d]["purchase"] / 1000 if d in ds.days else 0.0
            miss = 1 - ds.cov[d]
            if miss > 0.001:      # filled with the same week of another year: the system in normal operation
                ref, label = ds._ref(d)
                filled += miss * ref["purchase"] / 1000
                missing += miss
                labels.add(label)
            actual += billed[d]["known"] + billed[d]["est"]
            actual_known_days += billed[d]["known_frac"]
        t = tariffs[-1]   # the price that applies now
        to_year = 365 / n

        def offer(kwh: float) -> dict:
            rounded = math.ceil(kwh / 50) * 50
            yearly = 12 * t["base_eur_month"] + rounded * t["price_ct_kwh"] / 100
            return {"kwh": round(kwh, 1), "kwh_rounded": rounded, "monthly_kwh": round(rounded / 12, 1),
                    "yearly_cost": round(yearly, 2), "monthly_payment": math.ceil(yearly / 12)}

        sonnen_normal = (recorded + filled) * to_year
        normal = offer(sonnen_normal * (factor["value"] if factor else 1.0))
        out["proposal"] = {
            "from": start.isoformat(), "to": end.isoformat(), "days": n, "extrapolated": n < 360,
            "recorded_kwh": round(recorded, 1), "filled_kwh": round(filled, 1), "missing_days": round(missing, 1),
            "reference": ", ".join(sorted(labels)), "sonnen_normal_kwh": round(sonnen_normal, 1),
            "basis": "meter" if factor else "sonnen",
            # what to report: a normal year, as the meter would count it
            "annual_kwh": normal["kwh"], "annual_kwh_rounded": normal["kwh_rounded"], "monthly_kwh": normal["monthly_kwh"],
            "yearly_cost": normal["yearly_cost"], "monthly_payment": normal["monthly_payment"],
            # what the meter really counted in these twelve months (outages included)
            "actual": {**offer(actual * to_year), "known_days": round(actual_known_days, 1)},
            "base_eur_month": t["base_eur_month"], "price_ct_kwh": t["price_ct_kwh"],
        }
    return out
