# Changelog

## 1.0.0
- Initial release.
- Local poller for the Enphase IQ Gateway (firmware 7/8, token auth).
- Enables the `/ivp/livedata/stream` live feed and publishes PV, house load,
  grid (signed, plus split import/export) and battery power from
  `/ivp/livedata/status`.
- Energy totals (today + lifetime) and microinverter count from
  `/production.json`; grid voltage and frequency from `/ivp/meters/readings`.
- In-code token lifecycle: uses a cached token, a manually pasted token, or
  mints/refreshes one from the Enphase cloud (Enlighten -> Entrez) on a 401 or
  when within 7 days of expiry.
- MQTT auto-discovery; gateway connectivity binary sensor.
- `--probe` one-shot for commissioning.
