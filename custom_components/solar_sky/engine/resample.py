"""Time-weighted window means from a state-change series."""

from __future__ import annotations

import math

import numpy as np

from .learner import STEP


def window_means(
    times: np.ndarray,
    values: np.ndarray,
    start: float,
    end: float,
    step: float = STEP,
    min_coverage: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Time-weighted mean of a step function over fixed windows.

    times/values describe a step function: values[i] holds from times[i] until times[i+1].
    A NaN value is a gap (sensor unavailable). Windows are [w, w + step) for w = start, start+step, ...
    up to end; each result is stamped at its window END. Windows covered less than min_coverage
    give NaN.
    """
    times = np.asarray(times, dtype=float)
    values = np.asarray(values, dtype=float)
    start = math.floor(start / step) * step
    edges = np.arange(start, end + 1e-9, step)
    if len(edges) < 2 or len(times) == 0:
        return np.array([]), np.array([])
    order = np.argsort(times, kind="stable")
    times, values = times[order], values[order]
    ok = np.isfinite(values)
    # cumulative integral of the step function and of its "available" indicator, at each change time
    dt = np.diff(times)
    seg_v = np.where(ok, values, 0.0)[:-1]
    seg_c = ok[:-1].astype(float)
    cum_v = np.concatenate([[0.0], np.cumsum(seg_v * dt)])
    cum_c = np.concatenate([[0.0], np.cumsum(seg_c * dt)])

    def at(cum: np.ndarray, rate: np.ndarray, t: np.ndarray) -> np.ndarray:
        i = np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(times) - 1)
        before = t < times[0]
        out = cum[i] + rate[i] * (t - times[i])
        return np.where(before, 0.0, out)

    rate_v = np.where(ok, values, 0.0)
    rate_c = ok.astype(float)
    iv = at(cum_v, rate_v, edges)
    ic = at(cum_c, rate_c, edges)
    integral = np.diff(iv)
    covered = np.diff(ic)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(covered >= min_coverage * step, integral / np.where(covered > 0, covered, 1), np.nan)
    return edges[1:], mean


class Accumulator:
    """Live version of window_means: integrates state changes as they arrive."""

    def __init__(self, step: float = STEP) -> None:
        self.step = step
        self._value: float = math.nan
        self._since: float | None = None
        self._integral = 0.0
        self._covered = 0.0

    def update(self, ts: float, value: float) -> None:
        self._advance(ts)
        self._value = value

    def _advance(self, ts: float) -> None:
        if self._since is not None and ts > self._since and math.isfinite(self._value):
            self._integral += self._value * (ts - self._since)
            self._covered += ts - self._since
        self._since = ts if self._since is None else max(self._since, ts)

    def close(self, ts: float, min_coverage: float = 0.5) -> float:
        """Finish the window ending at ts and return its mean (NaN if mostly unavailable)."""
        self._advance(ts)
        mean = self._integral / self._covered if self._covered >= min_coverage * self.step else math.nan
        self._integral = 0.0
        self._covered = 0.0
        return mean
