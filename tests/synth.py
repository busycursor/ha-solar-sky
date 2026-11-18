"""Synthetic PV site for tests, demos and screenshots. Everything here is made up.

A 5 kW array at a fictional location, with a planted horizon (a tree line to the south-west and a
low building to the east), weather that switches between clear, broken and overcast regimes, wind,
and an optional inverter limit. The true clear-sky index is returned alongside the power, so tests
can check what the learner recovers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from custom_components.solar_sky.engine.physics import Site, plane_power_shape

SITE = Site(latitude=40.0, longitude=-105.0, elevation_m=1600.0)  # a round number on a map, nothing more
TILT, AZIMUTH = 25.0, 170.0
CAPACITY = 1.6  # W per W/m2 (about 5 kWp)


def horizon_profile() -> np.ndarray:
    """Obstruction top elevation per 4 degrees of azimuth (90 values)."""
    h = np.zeros(90)
    az = np.arange(90) * 4 + 2
    h[(az >= 228) & (az <= 262)] = 20.0  # trees
    h[(az >= 236) & (az <= 250)] = 26.0
    h[(az >= 76) & (az <= 104)] = 12.0  # building
    return h


@dataclass
class Weather:
    seed: int = 7
    rng: np.random.Generator = field(init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def regimes(self, days: int) -> np.ndarray:
        """0 = clear, 1 = broken cloud, 2 = overcast, Markov from day to day."""
        p = np.array([[0.6, 0.3, 0.1], [0.35, 0.4, 0.25], [0.2, 0.4, 0.4]])
        out = np.zeros(days, dtype=int)
        for i in range(1, days):
            out[i] = self.rng.choice(3, p=p[out[i - 1]])
        return out


def simulate(
    start: float,
    days: int,
    step: float = 60.0,
    seed: int = 7,
    clip_w: float | None = None,
    site: Site = SITE,
    tilt: float = TILT,
    azimuth: float = AZIMUTH,
):
    """Return dict of arrays at `step` seconds: ts, power (W), index (true clear-sky index), temp, wind_dir, wind_kmh."""
    w = Weather(seed)
    rng = w.rng
    n = int(days * 86400 / step)
    ts = start + np.arange(n) * step
    reg = w.regimes(days + 1)
    day = ((ts - start) // 86400).astype(int)
    r = reg[day]
    # cloud field: smoothed noise per regime
    noise = rng.standard_normal(n)
    k = int(1800 / step)
    smooth = np.convolve(noise, np.ones(k) / k, mode="same") * np.sqrt(k)
    fast = np.convolve(rng.standard_normal(n), np.ones(5) / 5, mode="same") * 2
    broken = np.where(smooth + 0.6 * fast > 0.2, rng.uniform(0.25, 0.45, n), 1.0)
    overcast = np.clip(0.25 + 0.08 * smooth, 0.08, 0.5)
    haze = 1.0 - 0.02 * rng.random(days + 1)[day]
    index = np.where(r == 0, haze, np.where(r == 1, broken, overcast))
    solar_hour = ((ts + site.longitude * 240.0) % 86400) / 3600.0
    doy = ((ts / 86400.0) % 365.25) / 365.25
    season = np.sin(2 * np.pi * (doy - 0.3)) * (1 if site.latitude >= 0 else -1)
    temp = 12 + 10 * season + 6 * np.sin(2 * np.pi * (solar_hour - 9) / 24)
    model, aoi, sun = plane_power_shape(ts, site, tilt, azimuth, temp)
    hz = horizon_profile()[(sun.azimuth // 4).astype(int) % 90]
    shaded = sun.elevation < hz
    # shaded: direct beam blocked, roughly 15 % of clear-sky output left as diffuse
    vis = np.where(shaded, 0.15, 1.0)
    power = CAPACITY * model * index * vis * (1 + 0.004 * rng.standard_normal(n))
    power = np.where(sun.elevation > 0, np.maximum(power, 0), 0.0)
    if clip_w:
        power = np.minimum(power, clip_w)
    wind_dir = (250 + 40 * np.sin((ts - start) / 86400 / 3 * 2 * np.pi) + 10 * smooth) % 360
    wind_kmh = np.clip(12 + 6 * smooth + 8 * (r == 2), 0, None)
    return {
        "ts": ts,
        "power": power,
        "index": index * vis,
        "true_index": index,
        "shaded": shaded,
        "temp": temp,
        "wind_dir": wind_dir,
        "wind_kmh": wind_kmh,
        "regime": r,
        "elevation": sun.elevation,
    }


def five_minute(sim: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """5-minute window means stamped at window end (like the integration's own sampling)."""
    ts = sim["ts"]
    step = ts[1] - ts[0]
    per = int(300 / step)
    n = len(ts) // per * per
    t_end = ts[:n].reshape(-1, per)[:, 0] + 300
    p = sim["power"][:n].reshape(-1, per).mean(axis=1)
    t = sim["temp"][:n].reshape(-1, per).mean(axis=1)
    return t_end, p, t
