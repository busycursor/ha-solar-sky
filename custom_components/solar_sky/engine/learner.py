"""Cloud-index learner: turns 5-minute PV power into a clear-sky index, learning the site as it goes.

    index(t)    = P(t) / (k * model(t) * corr(sun bin))       1 = clear sky, 0 = no sunlight
    cloud cover = (1 - min(index, 1)) * 100 %

model   clear-sky plane-of-array power shape from physics.py for the configured tilt/azimuth
k       capacity scale in W per W/m2. Envelope follower over clear days: after a clear day,
        k = max(that day's level, decay * k), so it jumps up after rain or a cleaning and
        decays slowly while dirt builds.
corr    per 2x2 degree sun-position bin, median of P / (k_day * model) over recent clear days.
        It absorbs shading by trees and buildings, the true turbidity and anything else the
        physics gets wrong at that sun position.

A day counts as clear when the power ratio around solar noon is smooth (no cloud passing),
steady (no haze trend), and not far below the baseline. Clear days are archived (compressed)
for a year so the table can be rebuilt when the orientation changes.

Nothing in this module imports Home Assistant; the integration and the offline replay run the
same code.
"""

from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass, field

import numpy as np

from .physics import Site, plane_power_shape

STEP = 300.0  # seconds per sample
HALF = STEP / 2
BIN = 2.0  # degrees
STATE_VERSION = 1


@dataclass(slots=True)
class Settings:
    """Tunable constants. Defaults are the ones validated on ~2 years of data."""

    min_elevation: float = 10.0
    max_aoi: float = 75.0
    min_model: float = 50.0
    window_h: float = 2.0  # clear-day test window: solar noon +- this
    min_window_samples: int = 36
    rough_max: float = 0.01
    spread_max: float = 0.06
    rel_min: float = 0.95
    decay: float = 0.99  # baseline decay per clear day
    corr_days: int = 21  # clear days kept per bin
    corr_min_days: int = 3  # days before a bin counts as learned
    shade_floor: float = 0.75  # bins whose clear reference is below this are treated as shaded
    archive_days: int = 400
    linke: float = 3.0
    detrend: bool = True  # clear-day spread measured around a quadratic trend (orientation-independent)


@dataclass(slots=True)
class Reading:
    """One evaluated sample."""

    ts: float  # window end, UNIX seconds
    elevation: float
    azimuth: float
    index: float | None  # clear-sky index, None when unavailable
    expected_w: float | None  # clear-sky power expected now
    learned: bool  # the sun-position bin has enough clear days behind it
    reason: str | None = None  # why index is None

    @property
    def cloud_cover(self) -> float | None:
        if self.index is None:
            return None
        return (1.0 - min(self.index, 1.0)) * 100.0


def _pack(a: np.ndarray) -> str:
    return base64.b64encode(zlib.compress(np.ascontiguousarray(a).tobytes(), 9)).decode()


def _unpack(s: str, dtype: str) -> np.ndarray:
    return np.frombuffer(zlib.decompress(base64.b64decode(s)), dtype=dtype).copy()


def default_orientation(site: Site) -> tuple[float, float]:
    """A typical roof: tilted like a 6/12 pitch at most, facing the equator."""
    return min(26.0, abs(site.latitude)), (180.0 if site.latitude >= 0 else 0.0)


def bin_key(az: float, el: float) -> tuple[int, int]:
    """Bin index (az, el) for a sun position."""
    return int(az // BIN), int(el // BIN)


@dataclass(slots=True)
class ClearDay:
    """Raw samples of one clear day (kept so the table can be rebuilt)."""

    day: int  # mean-solar-time day number since epoch
    ts: np.ndarray  # int64 window-end timestamps
    power: np.ndarray  # float32 W
    temp: np.ndarray  # float32 C (nan when unknown)
    level: float = 0.0  # k for this day under the current orientation

    def to_dict(self) -> dict:
        t0 = int(self.ts[0])
        return {
            "day": self.day,
            "t0": t0,
            "dt": _pack(((self.ts - t0) // 60).astype("<u2")),
            "p": _pack(self.power.astype("<f4")),
            "t": _pack(self.temp.astype("<f4")),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ClearDay:
        ts = d["t0"] + _unpack(d["dt"], "<u2").astype("int64") * 60
        return cls(d["day"], ts, _unpack(d["p"], "<f4"), _unpack(d["t"], "<f4"))


@dataclass
class _DayBuffer:
    day: int
    ts: list[float] = field(default_factory=list)
    power: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)


class Learner:
    """Stateful learner. Feed it 5-minute mean power in time order."""

    def __init__(
        self,
        site: Site,
        tilt: float | None = None,
        azimuth: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.site = site
        self.s = settings or Settings()
        if tilt is None or azimuth is None:
            tilt, azimuth = default_orientation(site)
        self.tilt = float(tilt)
        self.azimuth = float(azimuth) % 360
        self.k: float | None = None
        self.k_max: float | None = None  # best baseline seen in the last year
        self.k_hist: list[tuple[int, float]] = []
        self.archive: list[ClearDay] = []
        self.corr: dict[tuple[int, int], list[float]] = {}
        self.corr_med: dict[tuple[int, int], float] = {}
        self.envelope: dict[tuple[int, int], list[float]] = {}  # per-bin daily q90 of P/(k*model), any weather
        self.days_seen = 0
        self.clear_days_total = 0
        self.last_ts: float | None = None
        self._buf: _DayBuffer | None = None
        self.last_day_result: dict | None = None

    # ------------------------------------------------------------------ evaluation

    def _day_of(self, ts: float) -> int:
        """Day number in local mean solar time, so a day never splits around noon."""
        return int((ts - HALF + self.site.longitude * 240.0) // 86400)

    def _corr_lookup(self, key: tuple[int, int], el: float) -> tuple[float | None, bool, str | None]:
        """Return (corr, learned, reason-if-unavailable)."""
        c = self.corr_med.get(key)
        if c is not None:
            if c < self.s.shade_floor:
                return None, True, "shaded"
            return c, True, None
        a, e = key
        nb = [self.corr_med[(a + da, e + de)] for da in (-1, 0, 1) for de in (-1, 0, 1) if (a + da, e + de) in self.corr_med]
        if len(nb) >= 3:
            c = float(np.mean(nb))
            if c < self.s.shade_floor:
                return None, False, "shaded"
            return c, False, None
        env = self.envelope.get(key)
        if env is not None and len(env) >= 5:
            q = float(np.quantile(env, 0.8))
            if q < 0.5:
                return None, False, "shaded"
            return min(1.0, q), False, None
        return 1.0, False, None

    def evaluate(self, ts: np.ndarray, power: np.ndarray, temp: np.ndarray | None = None) -> list[Reading]:
        """Evaluate samples against the current state without learning from them."""
        ts = np.asarray(ts, dtype=float)
        p = np.abs(np.asarray(power, dtype=float))
        model, aoi, sun = plane_power_shape(ts - HALF, self.site, self.tilt, self.azimuth, temp, linke=self.s.linke)
        out = []
        for i in range(len(ts)):
            el, az = float(sun.elevation[i]), float(sun.azimuth[i])
            reason = None
            if el < self.s.min_elevation:
                reason = "night" if el < 0 else "sun_low"
            elif aoi[i] > self.s.max_aoi or model[i] < self.s.min_model:
                reason = "edge_on"
            elif self.k is None:
                reason = "learning"
            elif not math.isfinite(p[i]):
                reason = "no_data"
            if reason:
                out.append(Reading(float(ts[i]), el, az, None, None, False, reason))
                continue
            c, learned, why = self._corr_lookup(bin_key(az, el), el)
            if c is None:
                out.append(Reading(float(ts[i]), el, az, None, None, learned, why))
                continue
            exp_w = self.k * model[i] * c
            idx = min(1.3, max(0.0, p[i] / exp_w))
            out.append(Reading(float(ts[i]), el, az, idx, exp_w, learned))
        return out

    # ------------------------------------------------------------------ learning

    def add(self, ts: np.ndarray, power: np.ndarray, temp: np.ndarray | None = None) -> list[Reading]:
        """Learn from samples (time ordered, window-end timestamps) and return their readings.

        Each sample is evaluated with the state as it was before its own day was closed,
        exactly like a live sensor would have reported it.
        """
        ts = np.asarray(ts, dtype=float)
        power = np.asarray(power, dtype=float)
        temp = np.full(len(ts), np.nan) if temp is None else np.asarray(temp, dtype=float)
        out: list[Reading] = []
        start = 0
        days = np.array([self._day_of(t) for t in ts], dtype=np.int64)
        while start < len(ts):
            d = days[start]
            end = start + int(np.searchsorted(days[start:], d, side="right"))
            if self._buf is not None and self._buf.day != d:
                self._close_day()
            if self._buf is None:
                self._buf = _DayBuffer(int(d))
            sl = slice(start, end)
            keep = ts[sl] > (self.last_ts or -np.inf)
            out.extend(r for r, k in zip(self.evaluate(ts[sl], power[sl], temp[sl]), keep) if k)
            b = self._buf
            b.ts.extend(ts[sl][keep].tolist())
            b.power.extend(np.abs(power[sl][keep]).tolist())
            b.temp.extend(temp[sl][keep].tolist())
            if keep.any():
                self.last_ts = float(ts[sl][keep][-1])
            start = end
        return out

    def flush(self) -> None:
        """Close the day in progress (used at the end of an offline replay)."""
        if self._buf is not None:
            self._close_day()

    def _ratios(self, ts: np.ndarray, power: np.ndarray, temp: np.ndarray):
        model, aoi, sun = plane_power_shape(ts - HALF, self.site, self.tilt, self.azimuth, temp, linke=self.s.linke)
        valid = (
            (sun.elevation >= self.s.min_elevation) & (aoi <= self.s.max_aoi) & (model >= self.s.min_model) & np.isfinite(power)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(valid, power / model, np.nan)
        return r, sun, valid

    def _day_test(self, ts: np.ndarray, r: np.ndarray, sun) -> dict:
        """Clear-day test on the solar-noon window. Returns stats and verdict."""
        w = np.abs(ts - HALF - sun.solar_noon) <= self.s.window_h * 3600
        w &= np.isfinite(r)
        res = {"n": int(w.sum()), "clear": False}
        if res["n"] < self.s.min_window_samples:
            res["why"] = "too_few_samples"
            return res
        x = r[w]
        # remove the known per-bin shape where learned, so a shaded corner doesn't fail a clear day
        keys = [bin_key(a, e) for a, e in zip(sun.azimuth[w], sun.elevation[w])]
        c = np.array([self.corr_med.get(k, 1.0) for k in keys])
        y = x / np.where(c > 0.3, c, 1.0)
        med = float(np.median(y))
        tw = ts[w]
        contiguous = np.diff(tw) <= STEP * 1.5
        dy = np.abs(np.diff(y))[contiguous]
        res["rough"] = float(np.median(dy) / med) if dy.size > 10 else 1.0
        if self.s.detrend:
            # remove a quadratic in time: what's left is cloud, not a wrong orientation guess
            h = (tw - tw.mean()) / 3600.0
            y = y / np.polyval(np.polyfit(h, y, 2), h) * med
        res["spread"] = float((np.quantile(y, 0.9) - np.quantile(y, 0.1)) / med)
        res["level"] = float(np.median(x))
        ref = self.k if self.k is not None else None
        res["rel"] = float(res["level"] / ref) if ref else 1.0
        res["clear"] = res["rough"] < self.s.rough_max and res["spread"] < self.s.spread_max and res["rel"] > self.s.rel_min
        return res

    def _close_day(self) -> None:
        b, self._buf = self._buf, None
        if b is None or not b.ts:
            return
        ts = np.array(b.ts)
        p = np.array(b.power)
        t = np.array(b.temp)
        r, sun, valid = self._ratios(ts, p, t)
        self.days_seen += 1
        res = self._day_test(ts, r, sun)
        res["day"] = b.day
        self.last_day_result = res
        level = res.get("level")
        # any-weather envelope per bin (spots shading before a clear day has been seen there)
        if self.k is not None and valid.any():
            v = r[valid] / self.k
            keys = [bin_key(a, e) for a, e in zip(sun.azimuth[valid], sun.elevation[valid])]
            per: dict[tuple[int, int], list[float]] = {}
            for kk, vv in zip(keys, v):
                per.setdefault(kk, []).append(vv)
            for kk, vals in per.items():
                lst = self.envelope.setdefault(kk, [])
                lst.append(float(np.quantile(vals, 0.9)))
                del lst[:-10]
        if not res["clear"]:
            return
        self.clear_days_total += 1
        day = ClearDay(b.day, ts.astype("int64"), p.astype("float32"), t.astype("float32"), level)
        self.archive.append(day)
        cutoff = b.day - self.s.archive_days
        self.archive = [a for a in self.archive if a.day >= cutoff]
        self._update_k(b.day, level)
        self._learn_day(day, r, sun, valid)

    def _update_k(self, day: int, level: float) -> None:
        self.k = level if self.k is None else max(level, self.s.decay * self.k)
        self.k_hist.append((day, round(self.k, 4)))
        self.k_hist = [x for x in self.k_hist if x[0] > day - 400]
        self.k_max = max(v for _, v in self.k_hist if _ > day - 365)

    def _learn_day(self, day: ClearDay, r: np.ndarray, sun, valid: np.ndarray) -> None:
        v = r[valid] / day.level
        keys = [bin_key(a, e) for a, e in zip(sun.azimuth[valid], sun.elevation[valid])]
        per: dict[tuple[int, int], list[float]] = {}
        for kk, vv in zip(keys, v):
            per.setdefault(kk, []).append(vv)
        for kk, vals in per.items():
            lst = self.corr.setdefault(kk, [])
            lst.append(float(np.median(vals)))
            del lst[: -self.s.corr_days]
            if len(lst) >= self.s.corr_min_days:
                self.corr_med[kk] = float(np.median(lst))

    def rebuild(self) -> None:
        """Recompute k and the table from the clear-day archive (after an orientation change)."""
        self.corr.clear()
        self.corr_med.clear()
        self.envelope.clear()
        self.k = None
        self.k_hist.clear()
        for day in sorted(self.archive, key=lambda d: d.day):
            ts = day.ts.astype(float)
            r, sun, valid = self._ratios(ts, day.power.astype(float), day.temp.astype(float))
            w = (np.abs(ts - HALF - sun.solar_noon) <= self.s.window_h * 3600) & np.isfinite(r)
            if w.sum() < self.s.min_window_samples:
                continue
            day.level = float(np.median(r[w]))
            self._update_k(day.day, day.level)
            self._learn_day(day, r, sun, valid)

    # ------------------------------------------------------------------ orientation

    def set_orientation(self, tilt: float, azimuth: float) -> None:
        """Change the plane and rebuild the table from the clear-day archive."""
        self.tilt, self.azimuth = float(tilt), float(azimuth) % 360
        self.rebuild()

    # ------------------------------------------------------------------ outputs

    def horizon(self) -> list[float]:
        """Obstruction top per 4 degrees of azimuth (90 values), from the learned table.

        For each azimuth column: the upper edge of the highest bin whose clear reference is below 0.5
        and that has a bin at or above 0.8 higher up. Zero where nothing has been found.
        """
        out = [0.0] * 90
        cols: dict[int, dict[int, float]] = {}
        for (a, e), c in self.corr_med.items():
            cols.setdefault(int(a * BIN // 4), {})[e] = c
        for col, es in cols.items():
            if not 0 <= col < 90:
                continue
            top = 0.0
            for e in sorted(es):
                if es[e] < 0.5 and any(es[u] >= 0.8 for u in es if u > e):
                    top = (e + 1) * BIN
            out[col] = top
        return out

    def seed_baseline(self, k: float) -> None:
        """Start from a baseline found elsewhere (the hourly backfill) until the first clear day."""
        if self.k is None and k and k > 0:
            self.k = float(k)

    def sun_path_bins(self, year_start: float) -> set[tuple[int, int]]:
        """All bins the sun crosses in a year where the model is valid (for learning progress)."""
        ts = year_start + np.arange(0, 366 * 86400, 600, dtype=float)
        model, aoi, sun = plane_power_shape(ts, self.site, self.tilt, self.azimuth, None, linke=self.s.linke)
        ok = (sun.elevation >= self.s.min_elevation) & (aoi <= self.s.max_aoi) & (model >= self.s.min_model)
        return {bin_key(a, e) for a, e in zip(sun.azimuth[ok], sun.elevation[ok])}

    def summary(self) -> dict:
        return {
            "tilt": round(self.tilt, 1),
            "azimuth": round(self.azimuth, 1),
            "baseline": round(self.k, 4) if self.k else None,
            "baseline_vs_best": round(self.k / self.k_max, 3) if self.k and self.k_max else None,
            "clear_days": self.clear_days_total,
            "archived_clear_days": len(self.archive),
            "days_seen": self.days_seen,
            "learned_bins": len(self.corr_med),
        }

    # ------------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        buf = None
        if self._buf is not None:
            buf = {
                "day": self._buf.day,
                "ts": self._buf.ts,
                "p": self._buf.power,
                "t": [None if math.isnan(x) else x for x in self._buf.temp],
            }
        return {
            "version": STATE_VERSION,
            "tilt": self.tilt,
            "azimuth": self.azimuth,
            "k": self.k,
            "k_hist": self.k_hist,
            "archive": [d.to_dict() for d in self.archive],
            "corr": {f"{a},{e}": v for (a, e), v in self.corr.items()},
            "envelope": {f"{a},{e}": v for (a, e), v in self.envelope.items()},
            "days_seen": self.days_seen,
            "clear_days_total": self.clear_days_total,
            "last_ts": self.last_ts,
            "buf": buf,
        }

    def load_dict(self, d: dict) -> None:
        if d.get("version") != STATE_VERSION:
            return
        self.archive = [ClearDay.from_dict(x) for x in d.get("archive", [])]
        self.days_seen = d.get("days_seen", 0)
        self.clear_days_total = d.get("clear_days_total", len(self.archive))
        self.last_ts = d.get("last_ts")
        key = lambda s: tuple(int(x) for x in s.split(","))  # noqa: E731
        orientation_changed = abs(d["tilt"] - self.tilt) > 1e-6 or abs(d["azimuth"] - self.azimuth) > 1e-6
        if orientation_changed:
            self.rebuild()
        else:
            self.k = d.get("k")
            self.k_hist = [tuple(x) for x in d.get("k_hist", [])]
            self.k_max = max((v for _, v in self.k_hist), default=None)
            self.corr = {key(s): v for s, v in d.get("corr", {}).items()}
            self.corr_med = {kk: float(np.median(v)) for kk, v in self.corr.items() if len(v) >= self.s.corr_min_days}
            self.envelope = {key(s): v for s, v in d.get("envelope", {}).items()}
            # day levels under the stored orientation
            for day in self.archive:
                ts = day.ts.astype(float)
                r, sun, _ = self._ratios(ts, day.power.astype(float), day.temp.astype(float))
                w = (np.abs(ts - HALF - sun.solar_noon) <= self.s.window_h * 3600) & np.isfinite(r)
                day.level = float(np.median(r[w])) if w.any() else 0.0
        if d.get("buf"):
            b = d["buf"]
            self._buf = _DayBuffer(b["day"], list(b["ts"]), list(b["p"]), [np.nan if x is None else x for x in b["t"]])
