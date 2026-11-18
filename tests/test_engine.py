"""Engine tests on a synthetic site (no Home Assistant needed)."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pytest

from custom_components.solar_sky.engine.hourly import backfill
from custom_components.solar_sky.engine.learner import Learner
from custom_components.solar_sky.engine.physics import Site, sun_position
from custom_components.solar_sky.engine.resample import Accumulator, window_means

from .synth import AZIMUTH, SITE, TILT, five_minute, horizon_profile, simulate

START = 1735689600.0  # 2025-01-01T00:00Z


@lru_cache
def year():
    sim = simulate(START, 365, step=60.0)
    return sim, five_minute(sim)


@lru_cache
def learned():
    _, (ts, p, t) = year()
    lr = Learner(SITE, TILT, AZIMUTH)
    out = lr.add(ts, p, t)
    lr.flush()
    return lr, out


def test_sun_position_equinox_noon():
    site = Site(40.0, 0.0)
    ts = np.arange(1742472000.0 - 3600, 1742472000.0 + 3600, 60.0)  # 2025-03-20 12:00Z +- 1 h
    sun = sun_position(ts, site)
    assert 49.5 < sun.elevation.max() < 50.6
    noon = ts[np.argmax(sun.elevation)]
    assert abs(noon - sun.solar_noon[0]) < 120
    assert abs(sun.azimuth[np.argmax(sun.elevation)] - 180) < 1.0


def test_sun_position_southern_hemisphere():
    sun = sun_position(np.array([1750420800.0]), Site(-33.9, 0.0))  # 2025-06-20 12:00Z
    assert abs(sun.azimuth[0]) < 2 or abs(sun.azimuth[0] - 360) < 2
    assert 32 < sun.elevation[0] < 34


def test_learner_recovers_true_index():
    sim, (ts, _, _) = year()
    _, out = learned()
    true = sim["true_index"][: len(ts) * 5].reshape(-1, 5).mean(1)
    shaded = sim["shaded"][: len(ts) * 5].reshape(-1, 5).any(1)
    idx = np.array([np.nan if r.index is None else r.index for r in out])
    ok = np.isfinite(idx) & ~shaded & (ts > ts[0] + 120 * 86400)
    assert ok.sum() > 10000
    assert np.corrcoef(idx[ok], true[ok])[0, 1] > 0.99
    assert np.quantile(np.abs(idx[ok] - true[ok]), 0.95) < 0.05


def test_shaded_positions_are_unavailable():
    sim, (ts, _, _) = year()
    _, out = learned()
    shaded = sim["shaded"][: len(ts) * 5].reshape(-1, 5).all(1) & (sim["elevation"][::5][: len(ts)] > 12)
    late = ts > ts[0] + 200 * 86400
    reported = np.array([r.index is not None for r in out])
    assert (reported & shaded & late).sum() < 0.05 * (shaded & late).sum()


def test_horizon_finds_the_tree_line():
    lr, _ = learned()
    got = np.array(lr.horizon())
    true = horizon_profile()
    trees = slice(57, 66)  # 228-262 deg
    assert np.all(np.abs(got[trees] - true[trees]) <= 4)
    assert got[:19].max() == 0 and got[30:55].max() == 0  # nothing invented elsewhere


def test_state_round_trip():
    lr, _ = learned()
    d = lr.to_dict()
    lr2 = Learner(SITE, TILT, AZIMUTH)
    lr2.load_dict(d)
    probe = np.arange(START + 200 * 86400, START + 201 * 86400, 300.0)
    p = np.full(len(probe), 3000.0)
    a = [r.index for r in lr.evaluate(probe, p)]
    b = [r.index for r in lr2.evaluate(probe, p)]
    assert a == b
    assert lr2.summary()["clear_days"] == lr.summary()["clear_days"]


def test_orientation_change_rebuilds_from_archive():
    lr, _ = learned()
    lr2 = Learner(SITE, TILT + 5, AZIMUTH - 10)
    lr2.load_dict(lr.to_dict())
    assert lr2.tilt == TILT + 5
    assert lr2.k is not None and len(lr2.corr_med) > 500


def test_wrong_orientation_still_learns():
    _, (ts, p, t) = year()
    lr = Learner(SITE, 15, 190)
    lr.add(ts, p, t)
    lr.flush()
    assert lr.summary()["clear_days"] > 100


def test_window_means_time_weighted():
    times = np.array([0.0, 100.0, 200.0, 650.0])
    vals = np.array([10.0, 20.0, np.nan, 40.0])
    ends, mean = window_means(times, vals, 0.0, 900.0)
    assert list(ends) == [300.0, 600.0, 900.0]
    assert mean[0] == pytest.approx((10 * 100 + 20 * 100) / 200)
    assert np.isnan(mean[1])  # unavailable for the whole window
    assert mean[2] == pytest.approx(40.0)  # 50 s missing, then 40


def test_accumulator_matches_window_means():
    acc = Accumulator()
    acc.update(0.0, 10.0)
    acc.update(100.0, 20.0)
    assert acc.close(300.0) == pytest.approx((10 * 100 + 20 * 200) / 300)
    assert acc.close(600.0) == pytest.approx(20.0)
    acc.update(700.0, float("nan"))
    assert np.isnan(acc.close(900.0))  # only 100 of 300 s known: below half -> nan


def test_hourly_backfill_tracks_truth():
    sim, _ = year()
    ts = sim["ts"]
    n = len(ts) // 60 * 60
    start = ts[:n].reshape(-1, 60)[:, 0]
    pw = sim["power"][:n].reshape(-1, 60)
    rows, k = backfill(SITE, TILT, AZIMUTH, start, pw.mean(1), pw.min(1), pw.max(1))
    true = sim["true_index"][:n].reshape(-1, 60).mean(1)
    shaded = sim["shaded"][:n].reshape(-1, 60).any(1)
    idx = np.array([np.nan if r.index is None else r.index for r in rows])
    ok = np.isfinite(idx) & ~shaded
    assert k is not None
    assert ok.sum() > 1000
    assert np.corrcoef(idx[ok], true[ok])[0, 1] > 0.97


@pytest.mark.parametrize("lon", [175.0, -158.0, 0.0])
def test_solar_noon_is_the_nearest_one(lon):
    ts = np.arange(START, START + 2 * 86400, 300.0)
    sun = sun_position(ts, Site(-41.0, lon))
    assert np.abs(ts - sun.solar_noon).max() <= 12 * 3600 + 60


def test_learns_near_the_date_line():
    site = Site(-41.3, 174.8)
    sim = simulate(START, 120, step=60.0, site=site, tilt=25, azimuth=0)
    ts, p, t = five_minute(sim)
    lr = Learner(site, 25, 0)
    lr.add(ts, p, t)
    lr.flush()
    assert lr.summary()["clear_days"] > 20


def test_clipping_reads_as_clear_not_cloudy():
    sim = simulate(START + 150 * 86400, 60, step=60.0, clip_w=1100.0)
    ts, p, t = five_minute(sim)
    lr = Learner(SITE, TILT, AZIMUTH)
    out = lr.add(ts, p, t)
    clipped = p >= 1100.0 * 0.999
    idx = np.array([np.nan if r.index is None else r.index for r in out])
    m = clipped & np.isfinite(idx) & (ts > ts[0] + 20 * 86400)
    assert m.sum() > 100
    assert np.median(idx[m]) > 0.9
