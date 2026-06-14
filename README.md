# Enphase Local Control

A Home Assistant add-on repository for reading an **Enphase IQ Gateway**
(firmware 7/8, token auth) entirely over the local network — the same live
feed the Enphase app's "Live" view uses — and publishing it to Home Assistant
over MQTT auto-discovery.

Built in the same shape as [Solis-Local](https://github.com/stuartj1-1981/Solis-Local)
and CosyLocal: a single-purpose poller, S6 service, MQTT discovery.

## Add-ons

### Enphase IQ Gateway Local (`enphase-envoy-local`)

Read-only poller. Enables the gateway's live data stream and publishes:

| Entity | Source | Notes |
|---|---|---|
| PV Production (W) | `/ivp/livedata/status` | live, ~1 Hz |
| House Load (W) | `/ivp/livedata/status` | derived by the gateway (production − grid) |
| Grid Power (W) | `/ivp/livedata/status` | **+ import / − export** |
| Grid Import / Export (W) | `/ivp/livedata/status` | split, each ≥ 0 |
| Battery Power (W) | `/ivp/livedata/status` | **+ discharge / − charge** |
| Battery SOC (%) | `/ivp/livedata/status` | 0 if no IQ Battery |
| PV / House Consumption — Today + Lifetime (kWh) | `/production.json` | energy counters |
| Microinverters Online | `/production.json` | active microinverter count |
| Grid Voltage (V), Grid Frequency (Hz) | `/ivp/meters/readings` | from the production CT |

All power values in the gateway's livedata are milliwatts; the add-on converts
to watts. A "Gateway Status" connectivity sensor tracks the add-on's link.

## Install

1. Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   add `https://github.com/stuartj1-1981/Enphase-Local`.
2. Install **Enphase IQ Gateway Local**, open **Configuration**.
3. Set `envoy_host` to your gateway (`envoy.local` or its IP). Leave
   `envoy_serial` empty to auto-detect.
4. For hands-off token renewal, set `enlighten_email` + `enlighten_password`
   (your Enphase Enlighten cloud login). The add-on mints a local token and
   refreshes it automatically.
5. Start the add-on. Entities appear under a single **Enphase IQ Gateway**
   device once MQTT discovery fires.

## Token handling

Firmware 7/8 gateways require a JWT; minting one always touches the Enphase
cloud, but all data access is local. The add-on, in order of preference:

1. reuses a cached token (`/data/enphase_token.jwt`),
2. uses a pasted `manual_token`, or
3. mints a fresh one from `enlighten_email` + `enlighten_password`.

It refreshes automatically when the token is within 7 days of expiry or a
request returns 401. A homeowner token lasts ~1 year. If you only supply a
`manual_token` (no credentials), grab one from
`https://enlighten.enphaseenergy.com/entrez-auth-token?serial_num=<serial>`
and update it before it expires.

## Commissioning

Run a one-shot read without MQTT:

```
ENLIGHTEN_EMAIL=you@example.com ENLIGHTEN_PASSWORD=... \
  python3 rootfs/opt/enphase/enphase_envoy.py --host 192.168.x.x --probe
```

## Not yet included

IQ Battery / Ensemble detail (`/ivp/ensemble/inventory`, `/ivp/ensemble/secctrl`
for true battery SOC and charge/discharge) — add if a battery is present.
