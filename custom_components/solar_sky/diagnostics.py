"""Diagnostics: the learner's state summary (no location, no raw history)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SolarSkyConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SolarSkyConfigEntry) -> dict[str, Any]:
    rt = entry.runtime_data
    lr = rt.learner
    r = rt.reading
    return {
        "options": {k: v for k, v in rt.opts.items()},
        "summary": rt.summary,
        "progress": rt.progress,
        "bootstrap": rt.bootstrap_status,
        "backfilled_hours": rt.backfilled_hours,
        "last_day": lr.last_day_result,
        "horizon": lr.horizon(),
        "reading": None
        if r is None
        else {"index": r.index, "learned": r.learned, "reason": r.reason, "elevation": r.elevation, "azimuth": r.azimuth},
        "settings": {k: getattr(lr.s, k) for k in lr.s.__slots__},
    }
