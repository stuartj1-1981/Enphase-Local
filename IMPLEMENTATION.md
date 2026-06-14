# Enphase IQ Gateway Local — Implementation Instruction

Build a Home Assistant add-on that polls an **Enphase IQ Gateway** over the LAN
(firmware 7/8, JWT token auth), enables its live data stream, and publishes
power + energy to Home Assistant via **MQTT auto-discovery**. Read-only.
Token lifecycle handled **in code** (auto-refresh). Match the structure and
conventions of the existing `Solis-Local` / `CosyLocal` add-ons.

A verified reference implementation already exists in this folder
(`enphase-envoy-local/`); this document is the spec it satisfies. Implement to
the spec, then run the verification in §9 — it must pass.

---

## 1. Target device (reference unit)

- IQ Gateway, **metered** (`imeter=true`), single-phase ~240 V / 50 Hz.
- Serial `<your-gateway-serial>`, software `D8.x` → JWT "web token" auth regime.
- 13 microinverters; **2 CTs** (production + net/grid). House "load" is
  gateway-derived (production − grid), **not** a third CT.
- No active battery on this unit (storage power 0). Keep battery sensors; they
  read 0 until an IQ Battery is present.

## 2. Conventions (do not deviate)

- HA add-on; Alpine python base images (copy `build.yaml` from Solis-Local).
- **S6 service** + `bashio` run script: read options → export env → `exec python3`.
- HTTP via Python **stdlib `urllib`** (no `requests`). Only pip dependency is
  `paho-mqtt==1.6.1`.
- Gateway uses a **self-signed cert** → disable TLS verification **for gateway
  calls only** (`ssl.CERT_NONE`). Cloud calls use normal verification.
- One HA device, MQTT availability topic, retained discovery + state.

## 3. Repository layout

```
Enphase-Local/
  repository.json
  README.md
  enphase-envoy-local/
    config.yaml
    build.yaml
    Dockerfile
    CHANGELOG.md
    translations/en.json
    rootfs/etc/services.d/enphase-envoy/run
    rootfs/etc/services.d/enphase-envoy/finish
    rootfs/opt/enphase/enphase_envoy.py
```

`Dockerfile` installs `paho-mqtt==1.6.1`, `COPY rootfs /`, and `chmod a+x` the
`run`, `finish`, and `enphase_envoy.py` files.

## 4. Add-on options (`config.yaml`)

`slug: enphase_envoy_local`, `services: [mqtt:need]`, `map: [share:rw, addon_config:rw]`,
`startup: application`, `boot: auto`. Options keys **must** equal schema keys.

| Option | Schema | Default | Purpose |
|---|---|---|---|
| `envoy_host` | `str` | `envoy.local` | Gateway host/IP |
| `envoy_serial` | `str?` | `""` | Serial; blank → auto from `/info.xml` |
| `enlighten_email` | `str?` | `""` | Cloud login for token mint/refresh |
| `enlighten_password` | `password?` | `""` | Cloud password |
| `manual_token` | `str?` | `""` | Pasted-token fallback |
| `mqtt_host` | `str?` | `""` | Blank → auto from HA MQTT service |
| `mqtt_port` | `port` | `1883` | |
| `mqtt_user` | `str?` | `""` | |
| `mqtt_pass` | `password?` | `""` | |
| `poll_interval` | `int(2,300)` | `5` | Seconds between cycles |
| `debug` | `bool` | `false` | Verbose logging |

The `run` script falls back to `bashio::services mqtt …` when `mqtt_host` is
empty, and exports `TOKEN_DIR=/data`, `LOG_DIR=/config`.

## 5. Endpoints

| Method | Path | Auth | Used for | Cadence |
|---|---|---|---|---|
| GET | `/info.xml` | none | serial, software, pn (device block) | startup |
| POST | `/ivp/livedata/stream` body `{"enable":1}` | Bearer | turn live stream on | setup + if `sc_stream≠enabled` |
| GET | `/ivp/livedata/status` | Bearer | live powers + soc + `connection.sc_stream` | each cycle |
| GET | `/production.json` | Bearer | energy totals + inverter count | each cycle |
| GET | `/ivp/meters/readings` | Bearer | grid voltage + frequency | each cycle |

Base URL `https://<envoy_host>`. All Bearer calls retry **once** after refreshing
the token on HTTP 401.

## 6. Sensor map (authoritative)

`oid` is the MQTT object id and the key the parse functions emit. All power
sensors `state_class=measurement`; energy `total_increasing`.

| oid | Name | Unit | device_class | Source → transform |
|---|---|---|---|---|
| `pv_power` | PV Production | W | power | livedata `meters.pv.agg_p_mw` → `round(/1000)` |
| `house_load` | House Load | W | power | livedata `meters.load.agg_p_mw` → `round(/1000)` (gateway-derived) |
| `grid_power` | Grid Power | W | power | livedata `meters.grid.agg_p_mw` → `round(/1000)`; **+import / −export** |
| `grid_import` | Grid Import | W | power | derived: `max(0, grid_power)` |
| `grid_export` | Grid Export | W | power | derived: `max(0, −grid_power)` |
| `battery_power` | Battery Power | W | power | livedata `meters.storage.agg_p_mw` → `round(/1000)`; **+discharge / −charge** |
| `battery_soc` | Battery SOC | % | battery | livedata `meters.soc` → `round` |
| `pv_today` | PV Generation Today | kWh | energy | production `eim/production.whToday` → `round(/1000,3)` |
| `pv_lifetime` | PV Generation Lifetime | kWh | energy | production `eim/production.whLifetime` → `round(/1000,3)` |
| `consumption_today` | House Consumption Today | kWh | energy | consumption `total-consumption.whToday` → `round(/1000,3)` |
| `consumption_lifetime` | House Consumption Lifetime | kWh | energy | consumption `total-consumption.whLifetime` → `round(/1000,3)` |
| `microinverters_online` | Microinverters Online | — | — | production `inverters.activeCount` (int) |
| `grid_voltage` | Grid Voltage | V | voltage | meters/readings: first entry with `voltage>100` → `round(,1)` |
| `grid_frequency` | Grid Frequency | Hz | frequency | same entry `.freq` → `round(,2)` |

**Units/signs:** livedata `agg_p_mw` is **milliwatts**. Grid sign validated by
power balance (PV − load − export = 0). Battery sign convention assumed
(+discharge/−charge), unverifiable until a battery flows.

## 7. Python structure (`enphase_envoy.py`)

Keep the parse functions **pure** (no I/O) so they are unit-testable.

- **Constants:** `VERSION`, `LOGIN_URL=https://enlighten.enphaseenergy.com/login/login.json`,
  `TOKENS_URL=https://entrez.enphaseenergy.com/tokens`, a `CERT_NONE` ssl context,
  `DEFAULT_CONFIG`, `SENSORS` (the §6 table).
- **Pure functions:** `http_request(method,url,…)` (urllib, returns `(status, body)`,
  catches `HTTPError`); `jwt_exp(token)` (base64url-decode payload, return `exp` or 0);
  `parse_livedata(js)`, `parse_production(js)`, `parse_meters(readings)` per §6.
- **`EnphaseClient`:** `detect_info()` (regex `/info.xml`), `_mint_cloud()`
  (login.json → `session_id` → entrez `tokens`), `ensure_token(force)` (§8),
  `api()/api_json()` (Bearer + 401-retry), `enable_livedata()`.
- **`MQTTPublisher`:** discovery (§ below) + `publish`, LWT `offline`, `expire_after`.
- **`EnphaseController`:** `setup()` (info → token → mqtt → enable stream),
  `poll_once()` (3 GETs → merge parse dicts), `run()` (setup-retry loop, then
  poll loop with exponential backoff 5→60 s, publish `online`/`offline`),
  `probe()` (one-shot print, no MQTT).
- **`main`:** env→config (`build_config`), `argparse` (`--host`, `--probe`,
  `--debug`), logging to stdout + `RotatingFileHandler` under `LOG_DIR`,
  SIGTERM/SIGINT → graceful stop.

## 8. Token lifecycle (`ensure_token(force)`)

Refresh margin = **7 days**. Order:

1. If `not force` and the in-memory token is valid (`exp − now > 7d`) → return it.
2. If `not force` and cached `/data/enphase_token.jwt` is valid → load + return.
3. If `not force` and `manual_token` is valid → use, cache, return.
4. **Mint:** if `enlighten_email` + `enlighten_password` set → `login.json`
   (form `user[email]`,`user[password]`) → `session_id` → POST `tokens`
   (JSON `session_id`,`serial_num`,`username`) → JWT; cache + return.
   (Resolve serial from `/info.xml` first if blank.)
5. **Fallback** if minting impossible: `manual_token` (any), else stale cached
   file, else raise.

`api()` calls `ensure_token(force=True)` and retries once on HTTP 401. The 7-day
margin means normal polling refreshes the token **before** expiry unattended.

## 9. MQTT discovery (format)

- Discovery config (retained JSON): `homeassistant/<component>/enphase_envoy/<oid>/config`
  — `<component>` = `sensor` for §6 rows, `binary_sensor` for Gateway Status.
- State: `enphase_envoy/<oid>/state`.
- Availability / status: `enphase_envoy/status` (`online`/`offline`); LWT retained `offline`.
- Each sensor payload: `name`, `unique_id=enphase_envoy_<oid>`, `state_topic`,
  `availability:[{topic:status}]`, `device`, `expire_after = max(120, poll*6)`,
  plus `unit_of_measurement`/`device_class`/`state_class`/`icon` where set.
- `device`: `identifiers:["enphase_envoy_local"]`, `name:"Enphase IQ Gateway"`,
  `manufacturer:"Enphase"`, `model` from `/info.xml` pn, `sw_version` from
  `/info.xml` software, `serial_number` = serial.
- Gateway Status: `binary_sensor`, `device_class:connectivity`,
  `entity_category:diagnostic`, on/off = `online`/`offline`.

## 10. Verification (must pass before "done")

1. `python3 -m py_compile enphase_envoy.py` → OK.
2. `config.yaml` + `build.yaml` parse as YAML; `options` keys **==** `schema` keys.
   `translations/en.json`, `repository.json` parse as JSON.
3. **Offline parse test** — feed these fixtures into the parse functions and
   assert the outputs exactly:

   Fixtures: livedata `pv.agg_p_mw=894450, load=571756, grid=-322694, storage=0, soc=0`;
   production `inverters.activeCount=13`, `eim/production whToday=17644.116
   whLifetime=24461605.116`, `total-consumption whToday=19963.024
   whLifetime=66764709.024`; meters `[{voltage:240.212,freq:49.938}, …]`.

   | oid | expected |
   |---|---|
   | pv_power | 894 |
   | house_load | 572 |
   | grid_power | −323 |
   | grid_import | 0 |
   | grid_export | 323 |
   | battery_power | 0 |
   | battery_soc | 0 |
   | pv_today | 17.644 |
   | pv_lifetime | 24461.605 |
   | consumption_today | 19.963 |
   | consumption_lifetime | 66764.709 |
   | microinverters_online | 13 |
   | grid_voltage | 240.2 |
   | grid_frequency | 49.94 |

4. `jwt_exp` returns the payload `exp`; `_valid()` true for a 2027 exp, false for junk.

## 11. Acceptance criteria

- [ ] Repo matches §3; `run`/`finish`/`*.py` are executable.
- [ ] Add-on starts, reads `/info.xml`, obtains a token (mint or manual/cache).
- [ ] Enables livedata; first poll publishes all 14 sensors (CTs present).
- [ ] Entities appear under one **Enphase IQ Gateway** device; Gateway Status
      tracks availability.
- [ ] Survives gateway/MQTT drop: exponential backoff, status → `offline`.
- [ ] Token auto-refreshes on 401 and within 7 days of expiry — unattended ~1 yr.
- [ ] §10 verification all green.
- [ ] `--probe` prints values without MQTT.

## 12. Out of scope / future

- No control/writes (read-only).
- IQ Battery / Ensemble (`/ivp/ensemble/inventory`, `/ivp/ensemble/secctrl`) for
  true SOC + charge/discharge — add when storage is fitted.
- Optional: feed production/grid CTs into the Predbat load model.

## Appendix — token minting (homeowner)

If supplying `manual_token`: log in at `https://enlighten.enphaseenergy.com`,
then open `https://enlighten.enphaseenergy.com/entrez-auth-token?serial_num=<serial>`
and copy **only** the JWT (`eyJ…`, two dots — no JSON wrapper). The programmatic
`login.json` route fails with `401 {"message":"Invalid"}` when the account has MFA;
credentials in the add-on still work for minting only if MFA is off, otherwise use
the manual token.
