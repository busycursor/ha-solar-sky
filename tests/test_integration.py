"""Integration tests: config flow, setup, live sampling, websocket info."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.solar_sky.const import CONF_AZIMUTH, CONF_BACKFILL, CONF_POWER, CONF_TILT, DOMAIN
from custom_components.solar_sky.engine.physics import plane_power_shape

from .synth import AZIMUTH, CAPACITY, SITE, TILT

POWER = "sensor.pv_power"


@pytest.fixture
async def site(hass: HomeAssistant):
    await hass.config.async_update(latitude=SITE.latitude, longitude=SITE.longitude, elevation=int(SITE.elevation_m))
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "W", "device_class": "power", "friendly_name": "PV power"})


async def test_config_flow_creates_entry(hass: HomeAssistant, site) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POWER: POWER, CONF_TILT: 25, CONF_AZIMUTH: 170, CONF_BACKFILL: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Solar Sky (PV power)"
    assert result["data"][CONF_POWER] == POWER
    await hass.async_block_till_done()


async def test_config_flow_rejects_non_power(hass: HomeAssistant, site) -> None:
    hass.states.async_set("sensor.energy", "5", {"unit_of_measurement": "kWh"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POWER: "sensor.energy", CONF_TILT: 25, CONF_AZIMUTH: 170, CONF_BACKFILL: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_POWER: "not_power"}


async def test_forecast_solar_plane_prefills(hass: HomeAssistant, site) -> None:
    fs = MockConfigEntry(
        domain="forecast_solar",
        subentries_data=[
            {
                "data": {"declination": 33, "azimuth": 205, "modules_power": 4000},
                "subentry_type": "plane",
                "title": "Roof",
                "unique_id": None,
            }
        ],
    )
    fs.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    defaults = {str(k): k.default() for k in result["data_schema"].schema if hasattr(k, "default") and callable(k.default)}
    assert defaults[CONF_TILT] == 33
    assert defaults[CONF_AZIMUTH] == 205


async def test_live_sampling_reports_cloud_cover(hass: HomeAssistant, site, freezer: FrozenDateTimeFactory) -> None:
    # midday in June at the synthetic site
    t = datetime(2025, 6, 20, 18, 0, 10, tzinfo=UTC)
    freezer.move_to(t)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data
    assert rt.bootstrap_status in ("done", "live_only")
    rt.learner.seed_baseline(CAPACITY)  # as if a clear day had already been seen
    ent = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_cloud_cover")
    assert ent

    # half of the clear-sky power for 15 minutes -> about 50 % cloud
    for _ in range(3):
        ts = (datetime.now(UTC)).timestamp()
        model, _, _ = plane_power_shape([ts], SITE, TILT, AZIMUTH, 20.0)
        hass.states.async_set(POWER, str(round(0.5 * CAPACITY * model[0])), {"unit_of_measurement": "W", "device_class": "power"})
        await hass.async_block_till_done()
        nxt = datetime.fromtimestamp((ts // 300 + 1) * 300, UTC)
        freezer.move_to(nxt)
        async_fire_time_changed(hass, nxt)
        await hass.async_block_till_done()
    state = hass.states.get(ent)
    assert state is not None and state.state not in ("unavailable", "unknown")
    assert 40 <= float(state.state) <= 60
    assert state.attributes["learned"] is False

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_power_in_kw_is_converted(hass: HomeAssistant, site, freezer: FrozenDateTimeFactory) -> None:
    t = datetime(2025, 6, 20, 18, 0, 10, tzinfo=UTC)
    freezer.move_to(t)
    hass.states.async_set(POWER, "0", {"unit_of_measurement": "kW", "device_class": "power"})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data
    rt.learner.seed_baseline(CAPACITY)
    model, _, _ = plane_power_shape([t.timestamp()], SITE, TILT, AZIMUTH, 20.0)
    hass.states.async_set(POWER, str(CAPACITY * model[0] / 1000), {"unit_of_measurement": "kW", "device_class": "power"})
    nxt = t + timedelta(minutes=5)
    freezer.move_to(nxt.replace(second=0))
    async_fire_time_changed(hass, nxt.replace(second=0))
    await hass.async_block_till_done()
    assert rt.reading is not None and rt.reading.index is not None
    assert rt.reading.index > 0.9
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_change_reloads(hass: HomeAssistant, site) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_TILT: 30, CONF_AZIMUTH: 180})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.learner.tilt == 30
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_websocket_info(hass: HomeAssistant, site, hass_ws_client) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "solar_sky/info"})
    msg = await client.receive_json()
    assert msg["success"]
    info = msg["result"][0]
    assert info["cloud_cover"] == er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_cloud_cover")
    assert info["weather_cloud_cover"] is None
    assert len(info["horizon"]) == 90
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_backfill_from_long_term_statistics(hass: HomeAssistant, site, freezer: FrozenDateTimeFactory) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics, statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    from .synth import simulate

    start = datetime(2025, 5, 1, tzinfo=UTC).timestamp()
    sim = simulate(start, 45, step=300.0)
    pw = sim["power"].reshape(-1, 12)
    rows = [
        {
            "start": datetime.fromtimestamp(start + i * 3600, UTC),
            "mean": float(pw[i].mean()),
            "min": float(pw[i].min()),
            "max": float(pw[i].max()),
        }
        for i in range(len(pw))
    ]
    meta = {
        "mean_type": StatisticMeanType.ARITHMETIC,
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": POWER,
        "unit_class": "power",
        "unit_of_measurement": "W",
    }
    async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)

    freezer.move_to(datetime.fromtimestamp(start + 46 * 86400, UTC))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    rt = entry.runtime_data
    assert rt.bootstrap_status == "done"
    assert rt.learner.k == pytest.approx(CAPACITY, rel=0.1)
    assert rt.backfilled_hours > 200
    ent = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_cloud_cover")
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, datetime.fromtimestamp(start, UTC), None, {ent}, "hour", None, {"mean", "min", "max"}
    )
    assert len(stats[ent]) == rt.backfilled_hours
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bootstrap_learns_from_recorder_states(hass: HomeAssistant, site, freezer: FrozenDateTimeFactory) -> None:
    from unittest.mock import patch

    from homeassistant.core import State

    from .synth import simulate

    start = datetime(2025, 6, 1, tzinfo=UTC).timestamp()
    days = 10
    sim = simulate(start, days, step=60.0)
    attrs = {"unit_of_measurement": "W", "device_class": "power"}
    states = [
        State(POWER, str(round(float(v), 1)), attrs, last_changed=datetime.fromtimestamp(t, UTC), last_reported=None)
        for t, v in zip(sim["ts"], sim["power"], strict=True)
    ]
    freezer.move_to(datetime.fromtimestamp(start + days * 86400, UTC))

    def fake_history(hass_, start_time, end_time, entity_id, **kw):
        return {entity_id: [s for s in states if s.last_changed >= start_time]} if entity_id == POWER else {}

    with patch("custom_components.solar_sky.runtime.history.state_changes_during_period", side_effect=fake_history):
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_POWER: POWER, CONF_TILT: TILT, CONF_AZIMUTH: AZIMUTH, CONF_BACKFILL: True}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data
    assert rt.bootstrap_status == "done"
    assert rt.learner.days_seen >= days - 1
    assert rt.learner.clear_days_total >= 2
    assert rt.learner.k == pytest.approx(CAPACITY, rel=0.1)  # no temperature sensor: k absorbs the cell temperature
    assert rt.backfilled_hours > 50  # hourly rows built from the replayed 5-minute readings
    assert await hass.config_entries.async_unload(entry.entry_id)
