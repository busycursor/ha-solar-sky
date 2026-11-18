"""Hourly backfill: cloud cover for the past from Home Assistant's long-term statistics.

Home Assistant keeps 5-minute detail only for the recorder's purge window (10 days by default),
but hourly mean/min/max of a power sensor for as long as the database lives. This module learns
a coarser version of the live model from those hourly rows, so the sky card has history from
day one:

    R_h       = mean power / mean clear-sky model over the hour
    clear day = the hours around solar noon are smooth (max/min close to the model's own change)
                and level (spread < 6 %), and not far below the baseline
    k         = envelope follower over clear-day levels (same rule as live)
    corr      = per 4 x 4 degree bin of the hour's mid sun position, median R_h / k_day on clear days
    index_h   = R_h / (k * corr)

The live 5-minute learner is more precise; the backfill only covers hours before it started.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .learner import Settings
from .physics import Site, plane_power_shape

HOUR = 3600.0
SUB = 12  # model evaluated every 5 minutes inside each hour
HBIN = 4.0


@dataclass(slots=True)
class HourRow:
    start: float  # UNIX seconds, hour start
    cover: float | None  # mean cloud cover %
    cover_min: float | None
    cover_max: float | None
    index: float | None


def backfill(
    site: Site,
    tilt: float,
    azimuth: float,
    start: np.ndarray,
    mean: np.ndarray,
    pmin: np.ndarray,
    pmax: np.ndarray,
    temp: np.ndarray | None = None,
    settings: Settings | None = None,
) -> tuple[list[HourRow], float | None]:
    """Hourly cloud cover for hourly power statistics (W), and the final baseline. Time ordered input."""
    s = settings or Settings()
    start = np.asarray(start, dtype=float)
    n = len(start)
    if n == 0:
        return [], None
    mean = np.abs(np.asarray(mean, dtype=float))
    lo = np.fmin(np.abs(np.asarray(pmin, dtype=float)), np.abs(np.asarray(pmax, dtype=float)))
    hi = np.fmax(np.abs(np.asarray(pmin, dtype=float)), np.abs(np.asarray(pmax, dtype=float)))
    t_air = np.full(n, np.nan) if temp is None else np.asarray(temp, dtype=float)
    offs = (np.arange(SUB) + 0.5) * HOUR / SUB
    tt = (start[:, None] + offs[None, :]).ravel()
    model, aoi, sun = plane_power_shape(tt, site, tilt, azimuth, np.repeat(t_air, SUB), linke=s.linke)
    model = model.reshape(n, SUB)
    el = sun.elevation.reshape(n, SUB)
    ok_sub = (el >= s.min_elevation) & (aoi.reshape(n, SUB) <= s.max_aoi) & (model >= s.min_model)
    full = ok_sub.all(axis=1) & np.isfinite(mean)
    m_mean = model.mean(axis=1)
    m_lo, m_hi = model.min(axis=1), model.max(axis=1)
    mid = SUB // 2
    el_mid = el[:, mid]
    az_mid = sun.azimuth.reshape(n, SUB)[:, mid]
    noon = sun.solar_noon.reshape(n, SUB)[:, mid]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(full, mean / m_mean, np.nan)
        # smoothness: measured relative range vs the model's own range over the hour
        rough = np.abs((hi - lo) / mean - (m_hi - m_lo) / m_mean)
    smooth = full & (rough < 0.06)
    keys = np.stack([(az_mid // HBIN).astype(int), (el_mid // HBIN).astype(int)], axis=1)
    day = ((start + HOUR / 2 + site.longitude * 240.0) // 86400).astype(int)
    window = np.abs(start + HOUR / 2 - noon) <= s.window_h * HOUR + 1

    k: float | None = None
    k_at = np.full(n, np.nan)
    corr: dict[tuple[int, int], list[float]] = {}
    corr_med: dict[tuple[int, int], float] = {}
    # pass 1: walk days in order; k and the table evolve like the live learner's
    for d in np.unique(day):
        idx = np.nonzero(day == d)[0]
        k_at[idx] = k if k is not None else np.nan
        wf = idx[window[idx] & full[idx]]
        w = wf[smooth[wf]]
        # one rough hour is allowed (a corner of the window may be shaded at this time of year)
        if len(w) < 3 or len(wf) - len(w) > 1:
            continue
        x = r[w]
        med = float(np.median(x))
        if (x.max() - x.min()) / med > s.spread_max * 1.5:
            continue
        if k is not None and med / k < s.rel_min:
            continue
        k = med if k is None else max(med, s.decay * k)
        for i in idx[smooth[idx]]:
            kk = (int(keys[i, 0]), int(keys[i, 1]))
            lst = corr.setdefault(kk, [])
            lst.append(float(r[i] / med))
            del lst[: -s.corr_days]
            if len(lst) >= s.corr_min_days:
                corr_med[kk] = float(np.median(lst))
    # hours before the first clear day use the first baseline found
    first = np.flatnonzero(np.isfinite(k_at))
    if first.size:
        k_at[: first[0]] = k_at[first[0]]
    out: list[HourRow] = []
    for i in range(n):
        if not full[i] or not np.isfinite(k_at[i]):
            out.append(HourRow(float(start[i]), None, None, None, None))
            continue
        kk = (int(keys[i, 0]), int(keys[i, 1]))
        c = corr_med.get(kk)
        if c is None:
            a, e = kk
            nb = [corr_med[(a + da, e + de)] for da in (-1, 0, 1) for de in (-1, 0, 1) if (a + da, e + de) in corr_med]
            c = float(np.mean(nb)) if len(nb) >= 3 else None
        if c is None or c < s.shade_floor:
            out.append(HourRow(float(start[i]), None, None, None, None))
            continue
        e_mean = k_at[i] * c
        ix = min(1.3, max(0.0, r[i] / e_mean))
        ix_hi = min(1.3, max(0.0, hi[i] / (e_mean * m_hi[i])))
        ix_lo = min(1.3, max(0.0, lo[i] / (e_mean * m_lo[i])))
        cov = lambda v: round((1.0 - min(v, 1.0)) * 100.0, 1)  # noqa: E731
        out.append(HourRow(float(start[i]), cov(ix), cov(max(ix, ix_hi)), cov(min(ix, ix_lo)), round(ix, 3)))
    return out, k
