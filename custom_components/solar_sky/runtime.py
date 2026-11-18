"""Runtime for one Solar Sky entry: sampling, learning, persistence, history bootstrap."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from homeassistant.components.recorder import get_instance, history, statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import PowerConverter, TemperatureConverter

from .const import CONF_AZIMUTH, CONF_BACKFILL, CONF_POWER, CONF_TEMPERATURE, CONF_TILT, DOMAIN, SAVE_DELAY, STORAGE_VERSION
from .engine.hourly import backfill
from .engine.learner import STEP, Learner, Reading
from .engine.physics import Site
from .engine.resample import Accumulator, window_means

_LOGGER = logging.getLogger(__name__)
BOOTSTRAP_DAYS = 60  # how far back to look for 5-minute detail (the recorder keeps 10 days by default)


def _to_float(state: State | None, convert: Callable[[float, str | None], float] | None = None) -> float:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
        return math.nan
    try:
        v = float(state.state)
    except ValueError:
        return math.nan
    if convert is not None:
        try:
            v = convert(v, state.attributes.get("unit_of_measurement"))
        except Exception:  # noqa: BLE001 -- unknown unit: treat as unusable
            return math.nan
    return v


def _power_w(v: float, unit: str | None) -> float:
    if unit in (None, "", UnitOfPower.WATT):
        return abs(v)
    return abs(PowerConverter.convert(v, unit, UnitOfPower.WATT))


def _temp_c(v: float, unit: str | None) -> float:
    if unit in (None, "", UnitOfTemperature.CELSIUS):
        return v
    return TemperatureConverter.convert(v, unit, UnitOfTemperature.CELSIUS)


class SolarSkyRuntime:
    """Owns the learner for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # options override data; an option set to None removes the optional input
        self.opts: dict[str, Any] = {k: v for k, v in {**entry.data, **entry.options}.items() if v is not None}
        self.site = Site(hass.config.latitude, hass.config.longitude, float(hass.config.elevation or 0))
        self.learner = Learner(self.site, float(self.opts[CONF_TILT]), float(self.opts[CONF_AZIMUTH]))
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.reading: Reading | None = None
        self.summary: dict[str, Any] = {}
        self.progress: float | None = None
        self.bootstrap_status = "pending"
        self.backfilled_hours = 0
        self._path_bins: set[tuple[int, int]] = set()
        self._acc = Accumulator()
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._backfilled = False
        self._pending: list[tuple[float, float, float]] = []  # live samples queued while the bootstrap runs

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        data = await self.store.async_load()
        if data and "learner" in data:
            await self.hass.async_add_executor_job(self.learner.load_dict, data["learner"])
            self._backfilled = bool(data.get("backfilled"))
            self.backfilled_hours = int(data.get("backfilled_hours", 0))
            self.bootstrap_status = "done" if self._backfilled else "pending"
        await self._refresh_derived()
        power = self.opts[CONF_POWER]
        self._acc.update(time.time(), _to_float(self.hass.states.get(power), _power_w))
        self._unsubs.append(async_track_state_change_event(self.hass, [power], self._on_power))
        self._unsubs.append(async_track_utc_time_change(self.hass, self._on_tick, minute=list(range(0, 60, 5)), second=0))

    async def async_bootstrap_if_needed(self) -> None:
        """First start: learn from the recorder's history and fill in past cloud cover."""
        if self._backfilled:
            return
        try:
            await self._bootstrap()
        except Exception:
            self.bootstrap_status = "live_only"
            _LOGGER.exception("Solar Sky: learning from history failed; continuing with live data only")
        self._backfilled = True
        if self.bootstrap_status != "done":
            self.bootstrap_status = "live_only"
        pending, self._pending = self._pending, []
        if pending:
            arr = np.array(pending, dtype=float)
            async with self._lock:
                readings = await self.hass.async_add_executor_job(self.learner.add, arr[:, 0], arr[:, 1], arr[:, 2])
            if readings:
                self.reading = readings[-1]
            await self._refresh_derived()
        self._save()
        self._notify()

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.store.async_save(self._data())

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _data(self) -> dict[str, Any]:
        return {"learner": self.learner.to_dict(), "backfilled": self._backfilled, "backfilled_hours": self.backfilled_hours}

    def _save(self) -> None:
        self.store.async_delay_save(self._data, SAVE_DELAY)

    async def _refresh_derived(self) -> None:
        if not self._path_bins:
            y0 = datetime(datetime.now(UTC).year, 1, 1, tzinfo=UTC).timestamp()
            self._path_bins = await self.hass.async_add_executor_job(self.learner.sun_path_bins, y0)
        learned = sum(1 for b in self._path_bins if b in self.learner.corr_med)
        self.progress = round(100.0 * learned / len(self._path_bins), 1) if self._path_bins else None
        self.summary = self.learner.summary()

    # ------------------------------------------------------------------ live sampling

    @callback
    def _on_power(self, event: Event[EventStateChangedData]) -> None:
        self._acc.update(event.time_fired.timestamp(), _to_float(event.data["new_state"], _power_w))

    def _temperature(self) -> float:
        ent = self.opts.get(CONF_TEMPERATURE)
        return _to_float(self.hass.states.get(ent), _temp_c) if ent else math.nan

    async def _on_tick(self, now: datetime) -> None:
        t_end = math.floor(now.timestamp() / STEP) * STEP
        p = self._acc.close(t_end)
        temp = self._temperature()
        if self.bootstrap_status in ("pending", "running"):
            self._pending.append((t_end, p, temp))
            return
        async with self._lock:
            if self.learner.last_ts is not None and t_end <= self.learner.last_ts:
                return
            closing = self.learner._buf is not None and self.learner._day_of(t_end) != self.learner._buf.day  # noqa: SLF001
            readings = await self.hass.async_add_executor_job(
                self.learner.add, np.array([t_end]), np.array([p]), np.array([temp])
            )
        if readings:
            self.reading = readings[-1]
        if closing or not self.summary:
            await self._refresh_derived()
            self._save()
        elif int(t_end) % 3600 == 0:
            self._save()
        self._notify()

    # ------------------------------------------------------------------ bootstrap

    async def _bootstrap(self) -> None:
        self.bootstrap_status = "running"
        self._notify()
        rec = get_instance(self.hass)
        now = time.time()
        end5 = math.floor(now / STEP) * STEP
        days = min(BOOTSTRAP_DAYS, int(getattr(rec, "keep_days", BOOTSTRAP_DAYS)) + 1)
        start = datetime.fromtimestamp(now, UTC) - timedelta(days=days)
        power_id = self.opts[CONF_POWER]
        temp_id = self.opts.get(CONF_TEMPERATURE)

        def load_states(entity_id: str, conv) -> tuple[np.ndarray, np.ndarray]:
            res = history.state_changes_during_period(
                self.hass, start, None, entity_id, no_attributes=False, include_start_time_state=True
            )
            sts = res.get(entity_id, [])
            return (
                np.array([s.last_changed.timestamp() for s in sts], dtype=float),
                np.array([_to_float(s, conv) for s in sts], dtype=float),
            )

        pt, pv = await rec.async_add_executor_job(load_states, power_id, _power_w)
        ts5 = p5 = t5 = np.array([])
        if len(pt):
            first = max(pt[0], start.timestamp())
            ts5, p5 = window_means(pt, pv, first, end5)
            if temp_id:
                tt, tv = await rec.async_add_executor_job(load_states, temp_id, _temp_c)
                _, t5 = window_means(tt, tv, first, end5, min_coverage=0.0) if len(tt) else (None, np.full(len(ts5), np.nan))
            else:
                t5 = np.full(len(ts5), np.nan)
        detail_from = float(ts5[0] - STEP) if len(ts5) else end5

        rows: list[StatisticData] = []
        if self.opts.get(CONF_BACKFILL, True):
            rows += await self._hourly_backfill(rec, detail_from)

        if len(ts5):
            async with self._lock:
                readings = await self.hass.async_add_executor_job(self.learner.add, ts5, p5, t5)
            rows += _hourly_rows_from_readings(readings, until=math.floor(now / 3600) * 3600)
            if readings:
                self.reading = readings[-1]
        # the live accumulator starts after the replay
        await self._refresh_derived()
        if rows:
            self._import_statistics(rows)
        self.backfilled_hours = len(rows)
        self.bootstrap_status = "done"
        _LOGGER.info(
            "Solar Sky: learned from %d five-minute samples and filled %d past hours; %s",
            len(ts5),
            len(rows),
            self.summary,
        )

    async def _hourly_backfill(self, rec, until: float) -> list[StatisticData]:
        power_id = self.opts[CONF_POWER]
        temp_id = self.opts.get(CONF_TEMPERATURE)
        end = datetime.fromtimestamp(math.floor(until / 3600) * 3600, UTC)
        ids = {power_id} | ({temp_id} if temp_id else set())

        def load():
            return statistics.statistics_during_period(
                self.hass,
                datetime(2000, 1, 1, tzinfo=UTC),
                end,
                ids,
                "hour",
                {"power": UnitOfPower.WATT, "temperature": UnitOfTemperature.CELSIUS},
                {"mean", "min", "max"},
            )

        stats = await rec.async_add_executor_job(load)
        prow = stats.get(power_id, [])
        if not prow:
            return []
        start = np.array([r["start"] for r in prow], dtype=float)
        get = lambda f: np.array([np.nan if r.get(f) is None else r[f] for r in prow], dtype=float)  # noqa: E731
        temp = None
        if temp_id and stats.get(temp_id):
            tmap = {r["start"]: r.get("mean") for r in stats[temp_id]}
            temp = np.array([np.nan if tmap.get(s) is None else tmap[s] for s in start], dtype=float)
        hours, k = await self.hass.async_add_executor_job(
            backfill, self.site, self.learner.tilt, self.learner.azimuth, start, get("mean"), get("min"), get("max"), temp
        )
        if k:
            self.learner.seed_baseline(k)
        return [
            StatisticData(start=datetime.fromtimestamp(h.start, UTC), mean=h.cover, min=h.cover_min, max=h.cover_max)
            for h in hours
            if h.cover is not None
        ]

    def cloud_cover_entity_id(self) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}_cloud_cover")

    def _import_statistics(self, rows: list[StatisticData]) -> None:
        entity_id = self.cloud_cover_entity_id()
        if not entity_id:
            return
        meta = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement="%",
        )
        statistics.async_import_statistics(self.hass, meta, sorted(rows, key=lambda r: r["start"]))


def _hourly_rows_from_readings(readings: list[Reading], until: float) -> list[StatisticData]:
    """Aggregate 5-minute readings into hourly mean/min/max cloud cover (hours with >= 3 values)."""
    by: dict[int, list[float]] = {}
    for r in readings:
        c = r.cloud_cover
        if c is None:
            continue
        h = int((r.ts - STEP) // 3600 * 3600)
        if h + 3600 > until:
            continue
        by.setdefault(h, []).append(c)
    return [
        StatisticData(
            start=datetime.fromtimestamp(h, UTC), mean=round(sum(v) / len(v), 1), min=round(min(v), 1), max=round(max(v), 1)
        )
        for h, v in sorted(by.items())
        if len(v) >= 3
    ]
