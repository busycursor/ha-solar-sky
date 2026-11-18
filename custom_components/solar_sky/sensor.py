"""Solar Sky sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.weather import ATTR_WEATHER_CLOUD_COVERAGE, ATTR_WEATHER_WIND_BEARING, ATTR_WEATHER_WIND_SPEED
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfSpeed
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import SpeedConverter

from . import SolarSkyConfigEntry
from .const import CONF_WEATHER, CONF_WIND_DIRECTION, CONF_WIND_SPEED, DOMAIN, VERSION
from .runtime import SolarSkyRuntime


async def async_setup_entry(hass: HomeAssistant, entry: SolarSkyConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    ents: list[SensorEntity] = [CloudCover(rt), ExpectedPower(rt), Baseline(rt), Performance(rt), Progress(rt)]
    weather = rt.opts.get(CONF_WEATHER)
    if weather:
        ents.append(WeatherMirror(rt, weather, "weather_cloud_coverage", ATTR_WEATHER_CLOUD_COVERAGE))
        if not rt.opts.get(CONF_WIND_DIRECTION):
            ents.append(WeatherMirror(rt, weather, "wind_direction", ATTR_WEATHER_WIND_BEARING))
        if not rt.opts.get(CONF_WIND_SPEED):
            ents.append(WeatherMirror(rt, weather, "wind_speed", ATTR_WEATHER_WIND_SPEED))
    add(ents)


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, rt: SolarSkyRuntime, key: str) -> None:
        self.rt = rt
        self._attr_unique_id = f"{rt.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, rt.entry.entry_id)},
            name=rt.entry.title,
            entry_type=DeviceEntryType.SERVICE,
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.rt.add_listener(self.async_write_ha_state))


class CloudCover(_Base):
    """Share of clear-sky sunlight not reaching the panels."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, rt: SolarSkyRuntime) -> None:
        super().__init__(rt, "cloud_cover")

    @property
    def available(self) -> bool:
        r = self.rt.reading
        return r is not None and r.index is not None

    @property
    def native_value(self) -> float | None:
        r = self.rt.reading
        return None if r is None or r.cloud_cover is None else round(r.cloud_cover, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = self.rt.reading
        if r is None:
            return {"reason": self.rt.bootstrap_status if self.rt.bootstrap_status != "done" else "waiting"}
        return {
            "clear_sky_index": None if r.index is None else round(r.index, 3),
            "learned": r.learned,
            "reason": r.reason,
            "sun_elevation": round(r.elevation, 1),
            "sun_azimuth": round(r.azimuth, 1),
        }


class ExpectedPower(_Base):
    """Power the panels would make right now under a clear sky."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, rt: SolarSkyRuntime) -> None:
        super().__init__(rt, "expected_clear_sky_power")

    @property
    def native_value(self) -> float | None:
        r = self.rt.reading
        if r is None:
            return None
        if r.expected_w is None:
            return 0.0 if r.reason == "night" else None
        return round(r.expected_w, 0)

    @property
    def available(self) -> bool:
        return self.native_value is not None


class Baseline(_Base):
    """Clear-sky capacity scale (W per W/m2); tracks soiling and aging."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, rt: SolarSkyRuntime) -> None:
        super().__init__(rt, "baseline")

    @property
    def native_value(self) -> float | None:
        k = self.rt.learner.k
        return None if k is None else round(k, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.rt.learner.last_day_result or {}
        return {
            **self.rt.summary,
            "history": self.rt.bootstrap_status,
            "backfilled_hours": self.rt.backfilled_hours,
            "last_day_clear": last.get("clear"),
        }


class Performance(_Base):
    """Today's clear-sky baseline relative to the best one in the last year."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, rt: SolarSkyRuntime) -> None:
        super().__init__(rt, "performance")

    @property
    def native_value(self) -> float | None:
        v = self.rt.summary.get("baseline_vs_best")
        return None if v is None else round(v * 100, 1)


class Progress(_Base):
    """Share of the year's sun path the clear-sky table has learned."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(self, rt: SolarSkyRuntime) -> None:
        super().__init__(rt, "learning_progress")

    @property
    def native_value(self) -> float | None:
        return self.rt.progress


class WeatherMirror(_Base):
    """Records one attribute of a weather entity, so it gets long-term statistics for the card."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, rt: SolarSkyRuntime, weather: str, key: str, attr: str) -> None:
        super().__init__(rt, key)
        self._weather = weather
        self._attr = attr
        self._attr_native_value = None
        if key == "wind_speed":
            self._attr_device_class = SensorDeviceClass.WIND_SPEED
            self._attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
        elif key == "wind_direction":
            self._attr_device_class = SensorDeviceClass.WIND_DIRECTION
            self._attr_state_class = SensorStateClass.MEASUREMENT_ANGLE
            self._attr_native_unit_of_measurement = "°"
        else:
            self._attr_native_unit_of_measurement = PERCENTAGE

    async def async_added_to_hass(self) -> None:
        self._update()
        self.async_on_remove(async_track_state_change_event(self.hass, [self._weather], self._changed))

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        self._update()
        self.async_write_ha_state()

    @callback
    def _update(self) -> None:
        st = self.hass.states.get(self._weather)
        v = st.attributes.get(self._attr) if st else None
        if not isinstance(v, (int, float)):
            self._attr_native_value = None
            self._attr_available = False
            return
        if self._attr == ATTR_WEATHER_WIND_SPEED:
            unit = st.attributes.get("wind_speed_unit") or UnitOfSpeed.KILOMETERS_PER_HOUR
            try:
                v = SpeedConverter.convert(v, unit, UnitOfSpeed.KILOMETERS_PER_HOUR)
            except Exception:  # noqa: BLE001
                self._attr_available = False
                return
        self._attr_available = True
        self._attr_native_value = round(float(v), 1)
