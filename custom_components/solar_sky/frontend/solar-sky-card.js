/* solar-sky-card: the sky over your solar panels, simulated from cloud cover the panels measured.
 *
 * Served and loaded automatically by the Solar Sky integration. Add it to a dashboard with
 *   type: custom:solar-sky-card
 * Options (all optional): entry_id, title, cloud_height_km (1.5), drift_factor (3), min_drift_kmh (15),
 *   view (auto | south_up | north_up), horizon (90 obstruction elevations, one per 4 deg of azimuth).
 *
 * Data, from Home Assistant long-term statistics (hourly mean/min/max):
 *   the integration's cloud cover sensor, its weather cloud coverage mirror (if a weather entity is set),
 *   and a wind direction/speed pair (sensors you chose, or the weather entity's wind).
 * Hours without a measurement are interpolated between measured hours, nudged toward the weather value when one exists.
 *
 * Simulation: one cloud layer, fBm noise thresholded so its sky coverage equals the hour's cloud cover;
 * the hour's min-max spread sets edge softness (uniform overcast = soft); it drifts with the wind, smoothed
 * with inertia (6 h time constant on direction vector and speed, at most 15 deg of turn per hour).
 * View: all-sky fisheye, zenith at the centre, the equator side at the top (as if facing the sun).
 */
(() => {
const VERSION = "0.9.0";
const HOUR = 3600e3, DAY = 24 * HOUR;

// ---------- sun position (SunCalc formulas) ----------
const rad = Math.PI / 180, e = rad * 23.4397;
function sunPos(ms, lat, lon) {
  const d = ms / DAY - 0.5 + 2440588 - 2451545;
  const M = rad * (357.5291 + 0.98560028 * d);
  const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  const L = M + C + rad * 102.9372 + Math.PI;
  const dec = Math.asin(Math.sin(e) * Math.sin(L)), ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
  const H = rad * (280.16 + 360.9856235 * d) - rad * -lon - ra, phi = rad * lat;
  const alt = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
  return { el: alt / rad, az: (az / rad + 180 + 360) % 360 };
}

// ---------- noise, identical in JS (for the coverage threshold) and GLSL ----------
const fract = x => x - Math.floor(x);
const hash = (x, y) => fract(Math.sin(x * 127.1 + y * 311.7) * 43758.5453);
function vnoise(x, y) {
  const ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
  const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
  const a = hash(ix, iy), b = hash(ix + 1, iy), c = hash(ix, iy + 1), d = hash(ix + 1, iy + 1);
  return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
}
function fbm(x, y) { let v = 0, amp = .5; for (let i = 0; i < 5; i++) { v += amp * vnoise(x, y); x = x * 2.02 + 17.3; y = y * 2.02 + 9.1; amp *= .5 } return v / .96875 }
const CDF = (() => { const s = []; for (let i = 0; i < 6000; i++) s.push(fbm(hash(i, 1) * 400, hash(i, 2) * 400)); return s.sort((a, b) => a - b) })();
const thresholdFor = cover => CDF[Math.min(CDF.length - 1, Math.max(0, Math.round((1 - cover) * (CDF.length - 1))))];

const VERT = `attribute vec2 p; varying vec2 uv; void main(){ uv = p; gl_Position = vec4(p, 0., 1.); }`;
const FRAG = `precision highp float;
varying vec2 uv;
uniform vec2 uOff;        // cloud field offset, km
uniform float uTh;        // coverage threshold
uniform float uSoft;      // edge softness (uniform overcast = soft)
uniform float uScale;     // features per km
uniform float uCover;     // 0..1
uniform vec2 uSun;        // azimuth, elevation (deg)
uniform float uHorizon[90];
uniform float uEst;       // 1 = estimated hour
uniform float uNorth;     // 1 = north at the top (southern hemisphere)
uniform float uH;         // cloud layer height, km
float hash(vec2 p){ return fract(sin(p.x*127.1 + p.y*311.7) * 43758.5453); }
float vnoise(vec2 p){ vec2 i = floor(p), f = p - i; vec2 u = f*f*(3.-2.*f);
  float a = hash(i), b = hash(i+vec2(1.,0.)), c = hash(i+vec2(0.,1.)), d = hash(i+vec2(1.,1.));
  return a + (b-a)*u.x + (c-a)*u.y + (a-b-c+d)*u.x*u.y; }
float fbm(vec2 p){ float v = 0., amp = .5; for(int i=0;i<5;i++){ v += amp*vnoise(p); p = p*2.02 + vec2(17.3, 9.1); amp *= .5; } return v/.96875; }
float horizonTop(float az){ int k = int(mod(az, 360.) / 4.); float t = 0.;
  for(int i=0;i<90;i++){ if(i==k) t = uHorizon[i]; } return t; }
void main(){
  float r = length(uv); if (r > 1.) { gl_FragColor = vec4(0.); return; }
  // fisheye: centre = zenith, edge = horizon; south up, east left
  float el = 90. * (1. - r);
  float az = uNorth > .5 ? mod(degrees(atan(uv.x, uv.y)) + 360., 360.)     // top = N, right = E, left = W
                        : mod(degrees(atan(-uv.x, -uv.y)) + 360., 360.);  // top = S, left = E, right = W
  float se = uSun.y;
  // sky colour by sun elevation
  vec3 dayZ = vec3(.24,.50,.86), dayH = vec3(.72,.84,.95), duskH = vec3(.98,.62,.36), nightZ = vec3(.03,.05,.12), nightH = vec3(.08,.11,.22);
  float day = smoothstep(-6., 8., se), dusk = (1. - smoothstep(0., 14., se)) * smoothstep(-8., 0., se);
  vec3 zen = mix(nightZ, dayZ, day), hor = mix(nightH, dayH, day);
  hor = mix(hor, duskH, dusk * .8);
  vec3 sky = mix(hor, zen, pow(el / 90., .6));
  // sun
  vec3 sd = vec3(cos(radians(uSun.y))*sin(radians(uSun.x)), sin(radians(uSun.y)), cos(radians(uSun.y))*cos(radians(uSun.x)));
  vec3 vd = vec3(cos(radians(el))*sin(radians(az)), sin(radians(el)), cos(radians(el))*cos(radians(az)));
  float ang = degrees(acos(clamp(dot(sd, vd), -1., 1.)));
  float sunUp = step(-1., se);
  // clouds: one layer at uH km
  float tanEl = tan(radians(max(el, 1.5)));
  vec2 pos = uH / tanEl * vec2(sin(radians(az)), cos(radians(az))) + uOff;
  float n = fbm(pos * uScale);
  float dens = smoothstep(uTh, uTh + uSoft, n);
  float thick = smoothstep(uTh, uTh + .25, n);
  // looking at the sun direction: how much cloud is in front of it
  vec2 spos = uH / tan(radians(max(se, 1.5))) * vec2(sin(radians(uSun.x)), cos(radians(uSun.x))) + uOff;
  float sunCloud = smoothstep(uTh, uTh + uSoft, fbm(spos * uScale));
  sky += sunUp * (1. - sunCloud) * (vec3(1., .9, .7) * .55 * exp(-ang / 9.) + vec3(1.) * (1. - smoothstep(.5, 1.2, ang)));
  vec3 lit = mix(vec3(1.), vec3(1., .78, .6), dusk);
  vec3 cloudCol = mix(lit * .98, vec3(.52,.55,.6) * mix(.25, 1., day), thick * (.4 + .6 * uCover));
  cloudCol = mix(vec3(.10,.11,.16), cloudCol, day);
  cloudCol += sunUp * vec3(1., .9, .75) * .35 * exp(-ang / 14.) * (1. - thick) * day;   // silver lining near the sun
  float haze = (1. - smoothstep(0., 18., el)) * .35;
  vec3 col = mix(sky, cloudCol, dens * (1. - haze * .5));
  col = mix(col, hor, haze * .4);
  // horizon learned from the panels (trees, buildings)
  if (el < horizonTop(az)) col = mix(vec3(.16,.28,.18), vec3(.06,.10,.07), 1. - day);
  // estimated hours: slight desaturation
  float g = dot(col, vec3(.299,.587,.114)); col = mix(col, vec3(g), uEst * .35);
  // rim
  col = mix(col, vec3(0.), smoothstep(.985, 1., r) * .6);
  gl_FragColor = vec4(col, 1.);
}`;

// ---------- every piece of text the card shows ----------
// Values in {braces} are filled in by the card; keep the braces, move them anywhere in the sentence.
const STRINGS = {
  title: "Sky over the panels",                 // default card title (the `title` option overrides it)
  choose_day: "Choose day",
  previous_month: "Previous month",
  previous_day: "Previous day",
  next_day: "Next day",
  next_month: "Next month",
  today: "Today",
  sky_label: "Simulated sky as seen from panels",
  play: "Play timelapse",
  pause: "Pause",
  hour_of_day: "Hour of day",
  grid_label: "Month: one row per day, one column per hour",
  legend_clear: "clear",
  legend_overcast: "overcast",
  legend_estimated: "estimated",
  legend_hint: "· dark = night · tap a cell",
  about: "Cloud cover is measured from the panels' output. Hours they can't see are interpolated, and follow the weather entity when set up. The cloud shapes are a simulation that matches each hour's cloud cover and the wind.",
  learning: "Still learning this site: {progress}% of the year's sun path so far. Unlearned sun positions are treated as unshaded.",
  no_integration: "No Solar Sky integration found. Add it under Settings → Devices & services.",
  no_webgl: "This browser has no WebGL.",
  cover: "{cover}% cloud",
  clear: "Clear",
  partly_cloudy: "Partly cloudy",
  mostly_cloudy: "Mostly cloudy",
  broken_clouds: "Broken clouds",
  overcast: "Overcast",
  sun: "sun {elevation}° up at {azimuth}°",
  drift: "clouds moving toward {direction}° at {speed} km/h",
  separator: " · ",
  measured: "Measured by the panels",
  night: "Night: estimated from the nearest daylight hours",
  estimated: "Estimated (the panels can't see this hour)",
  estimated_weather: "Estimated (the panels can't see this hour; weather service {weather}%)",
  no_data: "No data",
  nothing_near: "Nothing measured near this hour",
  loading: "Loading…",
  north: "N", east: "E", south: "S", west: "W",
  card_name: "Solar Sky",
  card_description: "Simulated all-sky view from cloud cover measured by your solar panels",
};
const fmt = (key, vals = {}) => STRINGS[key].replace(/\{(\w+)\}/g, (_, k) => vals[k] ?? "");

const DEFAULTS = { title: STRINGS.title, cloud_height_km: 1.5, drift_factor: 3, min_drift_kmh: 15, view: "auto" };
const KMH = { "km/h": 1, "mph": 1.609344, "m/s": 3.6, "kn": 1.852, "ft/s": 1.09728, "Beaufort": null };

class SolarSkyCard extends HTMLElement {
  static getStubConfig() { return {}; }
  setConfig(config) { this._config = { ...DEFAULTS, ...(config || {}) }; if (this._built) { this._monthKey = null; this._info = null; this._select(this._y, this._m, this._d, this._hour); } }
  getCardSize() { return 10; }
  getGridOptions() { return { columns: 12, rows: "auto", min_columns: 6 }; }
  set hass(h) {
    this._hass = h;
    if (!this._built) { this._build(); this._built = true; const now = new Date(); this._select(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + now.getMinutes() / 60); }
  }

  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
<style>
  ha-card { padding: 16px; display: grid; gap: 12px; }
  .top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
  h2 { margin: 0; font-size: 1.15rem; font-weight: 500; }
  .nav { display: flex; align-items: center; gap: 4px; }
  button { font: inherit; font-size: .85rem; color: var(--primary-text-color); background: var(--secondary-background-color, rgba(127,127,127,.12));
           border: 1px solid var(--divider-color); border-radius: 8px; min-width: 34px; height: 32px; padding: 0 10px; cursor: pointer; }
  button:hover { border-color: var(--secondary-text-color); }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .label { min-width: 9.5em; text-align: center; font-variant-numeric: tabular-nums; }
  .body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
  @media (max-width: 760px) { .body { grid-template-columns: minmax(0, 1fr); } }
  .skywrap { position: relative; width: 100%; aspect-ratio: 1; max-width: 520px; justify-self: center; }
  canvas.sky { width: 100%; height: 100%; display: block; }
  .compass { position: absolute; inset: 0; pointer-events: none; font-size: .75rem; color: var(--secondary-text-color); }
  .compass span { position: absolute; transform: translate(-50%, -50%); }
  .side { display: grid; gap: 10px; min-width: 0; }
  .grid { width: 100%; display: block; cursor: pointer; touch-action: manipulation; }
  .readout { font-variant-numeric: tabular-nums; display: grid; gap: 2px; }
  .readout b { font-size: 1.6rem; font-weight: 500; }
  .muted { color: var(--secondary-text-color); font-size: .82rem; }
  .play { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; }
  input[type=range] { width: 100%; accent-color: var(--primary-color); }
  .legend { display: flex; align-items: center; gap: 8px; font-size: .75rem; color: var(--secondary-text-color); flex-wrap: wrap; }
  .legend i { display: inline-block; width: 90px; height: 8px; border-radius: 2px; background: linear-gradient(90deg, #5aa9e6, #b9c4cf, #6e757d); }
  .legend .est { width: 14px; height: 8px; background: repeating-linear-gradient(45deg, #9aa4ae 0 2px, transparent 2px 4px); }
  .warn { color: var(--warning-color, #c77700); font-size: .85rem; }
</style>
<ha-card>
  <div class="top">
    <h2 id="title"></h2>
    <div class="nav" role="group" aria-label="${STRINGS.choose_day}">
      <button id="pm" title="${STRINGS.previous_month}" aria-label="${STRINGS.previous_month}">«</button>
      <button id="pd" title="${STRINGS.previous_day}" aria-label="${STRINGS.previous_day}">‹</button>
      <span class="label" id="dlabel"></span>
      <button id="nd" title="${STRINGS.next_day}" aria-label="${STRINGS.next_day}">›</button>
      <button id="nm" title="${STRINGS.next_month}" aria-label="${STRINGS.next_month}">»</button>
      <button id="today" title="${STRINGS.today}">${STRINGS.today}</button>
    </div>
  </div>
  <div class="warn" id="warn" hidden></div>
  <div class="body">
    <div class="skywrap">
      <canvas class="sky" id="sky" aria-label="${STRINGS.sky_label}"></canvas>
      <div class="compass" id="compass"></div>
    </div>
    <div class="side">
      <div class="readout">
        <b id="rcover">–</b>
        <span id="rline"></span>
        <span class="muted" id="rsrc"></span>
      </div>
      <div class="play">
        <button id="play" aria-label="${STRINGS.play}">▶</button>
        <input type="range" id="hour" min="0" max="23.99" step="0.05" aria-label="${STRINGS.hour_of_day}">
        <span class="muted" id="hlabel" style="min-width:4.5em;text-align:right"></span>
      </div>
      <canvas class="grid" id="grid" aria-label="${STRINGS.grid_label}"></canvas>
      <div class="legend"><span>${STRINGS.legend_clear}</span><i></i><span>${STRINGS.legend_overcast}</span><span class="est"></span><span>${STRINGS.legend_estimated}</span><span>${STRINGS.legend_hint}</span></div>
      <div class="muted" id="about">${STRINGS.about}</div>
      <div class="muted" id="learn"></div>
    </div>
  </div>
</ha-card>`;
    const $ = id => root.getElementById(id);
    this.$ = $;
    $("pm").onclick = () => this._shift(0, -1); $("nm").onclick = () => this._shift(0, 1);
    $("pd").onclick = () => this._shift(-1, 0); $("nd").onclick = () => this._shift(1, 0);
    $("today").onclick = () => { const n = new Date(); this._select(n.getFullYear(), n.getMonth(), n.getDate(), n.getHours() + n.getMinutes() / 60); };
    $("hour").oninput = e => { this._stop(); this._setHour(+e.target.value); };
    $("play").onclick = () => this._playing ? this._stop() : this._play();
    $("grid").onclick = e => this._gridClick(e);
    this._initGL($("sky"));
    new ResizeObserver(() => { this._sizeGL(); this._drawGrid(); }).observe($("sky"));
  }

  get _north() { const v = this._config.view; return v === "north_up" || (v === "auto" && this._hass && this._hass.config.latitude < 0); }

  _locale() { const l = this._hass && this._hass.locale; return (l && l.language) || this._hass?.language || navigator.language; }
  _hour12() { const f = this._hass && this._hass.locale && this._hass.locale.time_format;
    if (f === "12") return true; if (f === "24") return false;
    return new Intl.DateTimeFormat(f === "system" ? undefined : this._locale(), { hour: "numeric" }).resolvedOptions().hour12; }

  // ---------- integration info ----------
  async _loadInfo() {
    if (this._info) return this._info;
    let res = [];
    try { res = await this._hass.callWS({ type: "solar_sky/info", ...(this._config.entry_id ? { entry_id: this._config.entry_id } : {}) }); }
    catch (err) { console.warn("solar-sky-card", err); }
    this._info = (res && res[0]) || null;
    const w = this.$("warn");
    if (!this._info) { w.hidden = false; w.textContent = STRINGS.no_integration; }
    else w.hidden = true;
    const hz = (this._config.horizon && this._config.horizon.length === 90) ? this._config.horizon : (this._info ? this._info.horizon : null);
    if (this._gl && hz) this._gl.gl.uniform1fv(this._gl.U.uHorizon, new Float32Array(hz));
    if (this._info && this._info.progress != null && this._info.progress < 95)
      this.$("learn").textContent = fmt("learning", { progress: Math.round(this._info.progress) });
    else this.$("learn").textContent = "";
    return this._info;
  }

  // ---------- data ----------
  async _loadMonth(y, m) {
    const key = `${y}-${m}`; if (this._monthKey === key) return; this._monthKey = key;
    const info = await this._loadInfo();
    const start = new Date(y, m, 1).getTime(), end = new Date(y, m + 1, 1).getTime();
    const pad = DAY;   // a day either side for interpolation
    const IDS = { cover: info && info.cloud_cover, weather: info && info.weather_cloud_cover, wdir: info && info.wind_direction, wspd: info && info.wind_speed };
    const ids = Object.values(IDS).filter(Boolean);
    let res = {};
    if (ids.length) {
      try {
        res = (await this._hass.callWS({ type: "recorder/statistics_during_period", start_time: new Date(start - pad).toISOString(),
          end_time: new Date(end + pad).toISOString(), statistic_ids: ids, period: "hour", types: ["mean", "min", "max"] })) || {};
      } catch (err) { console.warn("solar-sky-card", err); }
    }
    if (this._monthKey !== key) return;
    const n = Math.round((end - start + 2 * pad) / HOUR), t0 = start - pad;
    const arr = (id, f) => { const a = new Array(n).fill(null); if (!id) return a; for (const r of res[id] || []) { const i = Math.round((r.start - t0) / HOUR); if (i >= 0 && i < n && r[f] != null) a[i] = r[f]; } return a; };
    const mean = arr(IDS.cover, "mean"), mn = arr(IDS.cover, "min"), mx = arr(IDS.cover, "max"), w = arr(IDS.weather, "mean");
    const wd = arr(IDS.wdir, "mean"), ws = arr(IDS.wspd, "mean");
    const toKmh = KMH[(info && info.wind_speed_unit) || "km/h"] ?? 1;
    const { latitude: lat, longitude: lon } = this._hass.config;
    const hours = [];
    for (let i = 0; i < n; i++) {
      const t = t0 + i * HOUR, sun = sunPos(t + HOUR / 2, lat, lon);
      hours.push({ t, sunEl: sun.el, meas: mean[i] != null, cover: mean[i], spread: mean[i] != null && mx[i] != null && mn[i] != null ? (mx[i] - mn[i]) : (mean[i] != null ? 20 : null), w: w[i], wd: wd[i], ws: ws[i] != null ? ws[i] * toKmh : null });
    }
    // fill: interpolate between measured hours (within 20 h), nudged toward the weather value when present
    const measured = hours.map((h, i) => h.meas ? i : -1).filter(i => i >= 0);
    for (let i = 0; i < n; i++) {
      const h = hours[i]; if (h.meas) continue;
      let a = -1, b = -1; for (const j of measured) { if (j < i) a = j; else { b = j; break; } }
      const ok = j => j >= 0 && Math.abs(j - i) <= 20;
      let c = null, s = null, dist = 99;
      if (ok(a) && ok(b)) { const f = (i - a) / (b - a); c = hours[a].cover + (hours[b].cover - hours[a].cover) * f; s = hours[a].spread + (hours[b].spread - hours[a].spread) * f; dist = Math.min(i - a, b - i); }
      else if (ok(a)) { c = hours[a].cover; s = hours[a].spread; dist = i - a; } else if (ok(b)) { c = hours[b].cover; s = hours[b].spread; dist = b - i; }
      if (h.w != null) { const wt = c == null ? 1 : Math.min(.7, dist / 6); c = c == null ? h.w : c * (1 - wt) + h.w * wt; if (s == null) s = 30; }
      h.cover = c; h.spread = s; h.est = c != null;
    }
    // cloud drift with inertia: the hourly wind becomes a vector (toward wd+180, times drift_factor for cloud height),
    // smoothed with a 6 h time constant; the drift direction may turn at most TURN deg per hour, so it bends, never reverses.
    const TAU = 6, TURN = 15, MINSPD = +this._config.min_drift_kmh, FACTOR = +this._config.drift_factor, F = 1 - Math.exp(-1 / TAU);
    let sx = null, sy = null, ss = null, dir = null;
    const drift = [];
    for (let i = 0; i < n; i++) {
      const h = hours[i];
      if (h.wd != null && h.ws != null) {
        const to = (h.wd + 180) * rad, sp = h.ws * FACTOR;
        const ux = Math.sin(to) * sp, uy = Math.cos(to) * sp;
        if (sx == null) { sx = ux; sy = uy; ss = sp } else { sx += (ux - sx) * F; sy += (uy - sy) * F; ss += (sp - ss) * F }
      }
      if (sx == null) { sx = 10; sy = 0; ss = 10 }               // no wind data: west wind, clouds drift east
      const target = (Math.atan2(sx, sy) / rad + 360) % 360, spd = Math.max(MINSPD, ss);
      if (dir == null) dir = target;
      else { let d = ((target - dir + 540) % 360) - 180; d = Math.max(-TURN, Math.min(TURN, d)); dir = (dir + d + 360) % 360; }
      drift.push({ dir, spd });
    }
    const cum = [[0, 0]];
    const vel = (i, f) => { const a = drift[i], b = drift[Math.min(n - 1, i + 1)];
      let d = ((b.dir - a.dir + 540) % 360) - 180; const dd = (a.dir + d * f) * rad, sp = a.spd + (b.spd - a.spd) * f;
      return [Math.sin(dd) * sp, Math.cos(dd) * sp]; };
    for (let i = 0; i < n; i++) { const [vx, vy] = vel(i, .5); cum.push([cum[i][0] + vx, cum[i][1] + vy]); }
    this._data = { t0, start, end, hours, drift, cum, vel, hasWeather: !!IDS.weather };
    this._drawGrid(); this._render();
  }

  _at(ms) {
    const d = this._data; if (!d) return null;
    const x = (ms - d.t0) / HOUR - .5, i = Math.floor(x), f = x - i;
    const A = d.hours[Math.max(0, Math.min(d.hours.length - 1, i))], B = d.hours[Math.max(0, Math.min(d.hours.length - 1, i + 1))];
    const lerp = (a, b) => a == null ? b : b == null ? a : a + (b - a) * f;
    const near = f < .5 ? A : B;
    return { cover: lerp(A.cover, B.cover), spread: lerp(A.spread, B.spread), est: !near.meas, w: near.w, meas: near.meas, hasData: near.cover != null };
  }

  _offset(ms) {
    const d = this._data; if (!d) return [0, 0];
    const x = Math.max(0, Math.min(d.hours.length - 1e-6, (ms - d.t0) / HOUR)), i = Math.floor(x), f = x - i;
    const [vx, vy] = d.vel(i, f / 2);
    return [d.cum[i][0] + vx * f, d.cum[i][1] + vy * f];
  }

  _driftAt(ms) { const d = this._data; if (!d) return null; const i = Math.max(0, Math.min(d.drift.length - 1, Math.floor((ms - d.t0) / HOUR))); return d.drift[i]; }

  // ---------- selection / playback ----------
  _select(y, m, d, hour) {
    const dt = new Date(y, m, d); this._y = dt.getFullYear(); this._m = dt.getMonth(); this._d = dt.getDate();
    this._hour = hour ?? this._hour ?? 12;
    this.$("title").textContent = this._config.title;
    this.$("dlabel").textContent = new Intl.DateTimeFormat(this._locale(), { weekday: "short", day: "numeric", month: "short", year: "numeric" }).format(dt);
    this.$("hour").value = this._hour;
    const N = this._north, c = this.$("compass");
    c.innerHTML = N ? `<span style="left:50%;top:3%">${STRINGS.north}</span><span style="left:97%;top:50%">${STRINGS.east}</span><span style="left:3%;top:50%">${STRINGS.west}</span><span style="left:50%;top:97%">${STRINGS.south}</span>`
                    : `<span style="left:50%;top:3%">${STRINGS.south}</span><span style="left:3%;top:50%">${STRINGS.east}</span><span style="left:97%;top:50%">${STRINGS.west}</span><span style="left:50%;top:97%">${STRINGS.north}</span>`;
    this._loadMonth(this._y, this._m).then(() => this._render());
    this._render(); this._drawGrid();
  }
  _shift(dd, dm) { this._select(this._y, this._m + dm, this._d + dd, this._hour); }
  _setHour(h) {
    if (h >= 24) { this._select(this._y, this._m, this._d + 1, h - 24); return; }
    this._hour = h; this.$("hour").value = h; this._render(); this._drawGrid();
  }
  _play() {
    this._playing = true; this.$("play").textContent = "❚❚"; this.$("play").setAttribute("aria-label", STRINGS.pause);
    let last = performance.now();
    const step = now => { if (!this._playing) return; const dt = (now - last) / 1000; last = now; this._setHour(this._hour + dt * 0.75); this._raf = requestAnimationFrame(step); };
    this._raf = requestAnimationFrame(step);
  }
  _stop() { this._playing = false; cancelAnimationFrame(this._raf); if (this.$) { this.$("play").textContent = "▶"; this.$("play").setAttribute("aria-label", STRINGS.play); } }
  disconnectedCallback() { this._stop(); }

  // ---------- month grid ----------
  _drawGrid() {
    const c = this.$ && this.$("grid"), d = this._data; if (!c) return;
    const days = new Date(this._y, this._m + 1, 0).getDate();
    const W = c.clientWidth || 300, L = 26, T = 14, cw = (W - L) / 24, ch = Math.max(6, Math.min(10, cw * .9)), H = T + days * ch;
    const dpr = window.devicePixelRatio || 1; c.width = W * dpr; c.height = H * dpr; c.style.height = H + "px";
    const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const cs = getComputedStyle(this); const txt = cs.getPropertyValue("--secondary-text-color").trim() || "#888";
    g.fillStyle = txt; g.font = "10px sans-serif"; g.textAlign = "center"; g.textBaseline = "top";
    for (const h of [0, 6, 12, 18]) g.fillText(h === 0 ? "0h" : String(h), L + h * cw + cw / 2, 0);
    g.textAlign = "right"; g.textBaseline = "middle";
    this._grid = { L, T, cw, ch, days };
    if (!d) return;
    const col = v => { const t = Math.max(0, Math.min(1, v / 100)); const a = [90,169,230], b = [185,196,207], k = [110,117,125];
      const [p, q, u] = t < .5 ? [a, b, t * 2] : [b, k, (t - .5) * 2]; return `rgb(${p.map((x, i) => Math.round(x + (q[i] - x) * u)).join(",")})`; };
    for (let day = 1; day <= days; day++) {
      const y0 = T + (day - 1) * ch;
      if (day % 5 === 0 || day === 1) { g.fillStyle = txt; g.fillText(String(day), L - 4, y0 + ch / 2); }
      for (let h = 0; h < 24; h++) {
        const ms = new Date(this._y, this._m, day, h).getTime(), i = Math.round((ms - d.t0) / HOUR), r = d.hours[i]; if (!r) continue;
        const x0 = L + h * cw;
        if (r.cover == null) { g.fillStyle = "rgba(127,127,127,.12)"; g.fillRect(x0, y0, cw - .5, ch - .5); continue; }
        g.fillStyle = col(r.cover); g.globalAlpha = r.sunEl > 0 ? 1 : .28; g.fillRect(x0, y0, cw - .5, ch - .5); g.globalAlpha = 1;
        if (!r.meas && r.sunEl > 0) { g.strokeStyle = "rgba(255,255,255,.55)"; g.lineWidth = 1; g.beginPath(); g.moveTo(x0, y0 + ch - .5); g.lineTo(x0 + Math.min(cw, ch), y0); g.stroke(); }
      }
    }
    g.strokeStyle = cs.getPropertyValue("--primary-text-color").trim() || "#000"; g.lineWidth = 1.5;
    g.strokeRect(L + Math.floor(this._hour) * cw - .5, T + (this._d - 1) * ch - .5, cw, ch);
  }
  _gridClick(e) {
    const gr = this._grid; if (!gr) return; const r = this.$("grid").getBoundingClientRect();
    const h = Math.floor((e.clientX - r.left - gr.L) / gr.cw), day = Math.floor((e.clientY - r.top - gr.T) / gr.ch) + 1;
    if (h < 0 || h > 23 || day < 1 || day > gr.days) return;
    this._stop(); this._select(this._y, this._m, day, h + .5);
  }

  // ---------- WebGL ----------
  _initGL(canvas) {
    const gl = canvas.getContext("webgl", { premultipliedAlpha: false, alpha: true, preserveDrawingBuffer: true });
    if (!gl) { canvas.replaceWith(Object.assign(document.createElement("div"), { textContent: STRINGS.no_webgl })); return; }
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(s)); return s; };
    const pr = gl.createProgram(); gl.attachShader(pr, sh(gl.VERTEX_SHADER, VERT)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FRAG)); gl.linkProgram(pr);
    if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) console.error(gl.getProgramInfoLog(pr));
    gl.useProgram(pr);
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(pr, "p"); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const U = {}; for (const n of ["uOff","uTh","uSoft","uScale","uCover","uSun","uHorizon","uEst","uNorth","uH"]) U[n] = gl.getUniformLocation(pr, n);
    gl.uniform1fv(U.uHorizon, new Float32Array(90));
    this._gl = { gl, U, canvas };
  }
  _sizeGL() { const g = this._gl; if (!g) return; const r = g.canvas.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    g.canvas.width = Math.max(64, Math.round(r.width * dpr)); g.canvas.height = Math.max(64, Math.round(r.height * dpr)); this._render(); }

  _render() {
    if (!this._hass || this._y == null) return;
    const ms = new Date(this._y, this._m, this._d).getTime() + this._hour * HOUR;
    const { latitude: lat, longitude: lon } = this._hass.config;
    const sun = sunPos(ms, lat, lon), st = this._at(ms);
    this.$("hlabel").textContent = new Intl.DateTimeFormat(this._locale(), { hour: "numeric", minute: "2-digit", hour12: this._hour12() }).format(new Date(ms));
    if (st && st.hasData) {
      this.$("rcover").textContent = fmt("cover", { cover: Math.round(st.cover) });
      const kind = STRINGS[st.cover < 10 ? "clear" : st.cover > 85 ? (st.spread < 15 ? "overcast" : "mostly_cloudy") : (st.spread > 40 ? "broken_clouds" : st.cover > 50 ? "mostly_cloudy" : "partly_cloudy")];
      const dr = this._driftAt(ms);
      this.$("rline").textContent = [kind, fmt("sun", { elevation: Math.round(sun.el), azimuth: Math.round(sun.az) }), ...(dr ? [fmt("drift", { direction: Math.round(dr.dir), speed: Math.round(dr.spd) })] : [])].join(STRINGS.separator);
      this.$("rsrc").textContent = st.meas ? STRINGS.measured : sun.el <= 0 ? STRINGS.night : st.w != null ? fmt("estimated_weather", { weather: Math.round(st.w) }) : STRINGS.estimated;
    } else {
      this.$("rcover").textContent = STRINGS.no_data; this.$("rline").textContent = fmt("sun", { elevation: Math.round(sun.el), azimuth: Math.round(sun.az) }); this.$("rsrc").textContent = this._data ? STRINGS.nothing_near : STRINGS.loading;
    }
    const g = this._gl; if (!g) return;
    const { gl, U } = g; gl.viewport(0, 0, g.canvas.width, g.canvas.height);
    const cover = st && st.hasData ? Math.max(0, Math.min(1, st.cover / 100)) : 0, spread = st && st.spread != null ? Math.max(0, Math.min(1, st.spread / 100)) : .3;
    const [ox, oy] = this._offset(ms);
    gl.uniform2f(U.uOff, ox, oy);
    gl.uniform1f(U.uTh, cover <= 0.002 ? 2 : thresholdFor(cover));
    gl.uniform1f(U.uSoft, .03 + .22 * (1 - spread) * cover);            // uniform overcast -> soft, broken -> crisp
    gl.uniform1f(U.uScale, .45);                                         // fixed: changing it moves the whole field
    gl.uniform1f(U.uCover, cover);
    gl.uniform2f(U.uSun, sun.az, sun.el);
    gl.uniform1f(U.uEst, st && !st.meas ? 1 : 0);
    gl.uniform1f(U.uNorth, this._north ? 1 : 0);
    gl.uniform1f(U.uH, +this._config.cloud_height_km || 1.5);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}
if (!customElements.get("solar-sky-card")) customElements.define("solar-sky-card", SolarSkyCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "solar-sky-card", name: STRINGS.card_name, description: STRINGS.card_description, preview: false });
console.info(`%c solar-sky-card ${VERSION} `, "background:#5aa9e6;color:#fff;border-radius:3px");
})();
