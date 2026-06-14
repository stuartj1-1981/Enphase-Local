#!/usr/bin/env python3
# =============================================================================
# Enphase IQ Gateway Local — Home Assistant add-on
# =============================================================================
# Local-network poller for the Enphase IQ Gateway (firmware 7/8, "web token"
# auth). Reads the same live feed the Enphase app's "Live" view uses and
# publishes it to Home Assistant over MQTT auto-discovery.
#
#   - Enables the live data stream (POST /ivp/livedata/stream {"enable":1}) and
#     reads /ivp/livedata/status (~1 Hz) for PV, house-load, grid and battery
#     power. Values there are in milliwatts -> divided to W here.
#   - Energy totals (today + lifetime) and the microinverter count come from
#     /production.json; grid voltage + frequency from /ivp/meters/readings.
#
# Design notes (why this exists rather than the core integration):
#   - ONE thread does every request; no shared state to race. Read-only — it
#     never writes to the gateway.
#   - Token lifecycle is handled IN CODE so the add-on is hands-off for a year+:
#     a cached token is reused; if it is missing, within 7 days of expiry, or
#     rejected with a 401, a fresh one is minted from the Enphase cloud
#     (Enlighten login -> Entrez token) using the stored Enlighten credentials,
#     or a manually pasted token is used as a fallback. The gateway itself is
#     reached purely locally; only token *minting* touches the cloud.
#   - Self-signed gateway cert -> TLS verification is disabled for the gateway
#     (LAN device, no hostname to verify).
#
# Sign conventions (validated against a live S-metered single-phase unit):
#   grid_power   +ve = importing, -ve = exporting   (import/export split too)
#   battery_power +ve = discharging, -ve = charging
#   pv_power / house_load are always >= 0
# =============================================================================

import os
import re
import sys
import time
import json
import ssl
import base64
import signal
import logging
import logging.handlers
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:  # pragma: no cover
    HAS_MQTT = False
    logging.warning("paho-mqtt not installed — MQTT publishing disabled")

VERSION = "1.0.0"

LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
TOKENS_URL = "https://entrez.enphaseenergy.com/tokens"

# Gateway uses a self-signed cert -> a TLS context that doesn't verify.
NOVERIFY = ssl.create_default_context()
NOVERIFY.check_hostname = False
NOVERIFY.verify_mode = ssl.CERT_NONE

UA = {"User-Agent": "enphase-envoy-local/%s" % VERSION}

# =============================================================================
# Defaults (overridden by environment variables from the S6 run script)
# =============================================================================
DEFAULT_CONFIG = {
    "envoy_host": "envoy.local",
    "envoy_serial": "",
    "enlighten_email": "",
    "enlighten_password": "",
    "manual_token": "",
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_base_topic": "enphase_envoy",
    "poll_interval": 5,
    "token_dir": "/data",
    "log_dir": "/config",
}

# Sensors published over MQTT discovery. oid -> the key the parse_* functions emit.
SENSORS = [
    {"oid": "pv_power",             "name": "PV Production",            "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:solar-power"},
    {"oid": "house_load",           "name": "House Load",               "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:home-lightning-bolt"},
    {"oid": "grid_power",           "name": "Grid Power",               "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:transmission-tower"},
    {"oid": "grid_import",          "name": "Grid Import",              "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:transmission-tower-import"},
    {"oid": "grid_export",          "name": "Grid Export",              "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:transmission-tower-export"},
    {"oid": "battery_power",        "name": "Battery Power",            "unit": "W",   "dclass": "power",       "sclass": "measurement",      "icon": "mdi:home-battery"},
    {"oid": "battery_soc",          "name": "Battery SOC",              "unit": "%",   "dclass": "battery",     "sclass": "measurement"},
    {"oid": "pv_today",             "name": "PV Generation Today",      "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "icon": "mdi:solar-power"},
    {"oid": "pv_lifetime",          "name": "PV Generation Lifetime",   "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "icon": "mdi:solar-power"},
    {"oid": "consumption_today",    "name": "House Consumption Today",  "unit": "kWh", "dclass": "energy",      "sclass": "total_increasing", "icon": "mdi:home-lightning-bolt"},
    {"oid": "consumption_lifetime", "name": "House Consumption Lifetime","unit": "kWh","dclass": "energy",      "sclass": "total_increasing", "icon": "mdi:home-lightning-bolt"},
    {"oid": "microinverters_online","name": "Microinverters Online",                                            "sclass": "measurement",      "icon": "mdi:solar-panel"},
    {"oid": "grid_voltage",         "name": "Grid Voltage",             "unit": "V",   "dclass": "voltage",     "sclass": "measurement"},
    {"oid": "grid_frequency",       "name": "Grid Frequency",           "unit": "Hz",  "dclass": "frequency",   "sclass": "measurement"},
]


class EnvoyError(Exception):
    """A gateway request failed (non-200 or non-JSON)."""


# =============================================================================
# Pure helpers (no I/O — unit-testable against captured payloads)
# =============================================================================
def http_request(method, url, headers=None, data=None, is_json=False, timeout=30, context=None):
    """Minimal urllib request. Returns (status_code, body_text)."""
    hdrs = dict(UA)
    hdrs.update(headers or {})
    body = None
    if data is not None:
        if is_json:
            body = json.dumps(data).encode()
            hdrs["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def jwt_exp(token):
    """Return the JWT 'exp' epoch, or 0 if the token can't be decoded."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode()))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


def parse_livedata(js):
    """PV / load / grid / battery power (W) + SOC from /ivp/livedata/status.
    agg_p_mw fields are milliwatts."""
    out = {}
    meters = js.get("meters") or {}

    def watts(section):
        sec = meters.get(section) or {}
        v = sec.get("agg_p_mw")
        return None if v is None else round(v / 1000.0)

    pv, load, grid, batt = watts("pv"), watts("load"), watts("grid"), watts("storage")
    if pv is not None:
        out["pv_power"] = pv
    if load is not None:
        out["house_load"] = load
    if grid is not None:
        out["grid_power"] = grid              # +import / -export
        out["grid_import"] = max(0, grid)
        out["grid_export"] = max(0, -grid)
    if batt is not None:
        out["battery_power"] = batt           # +discharge / -charge
    soc = meters.get("soc")
    if isinstance(soc, (int, float)):
        out["battery_soc"] = round(soc)
    return out


def parse_production(js):
    """Energy totals (kWh) + microinverter count from /production.json (Wh fields)."""
    out = {}
    for p in js.get("production", []) or []:
        t = p.get("type")
        if t == "eim" and p.get("measurementType", "production") == "production":
            if p.get("whToday") is not None:
                out["pv_today"] = round(p["whToday"] / 1000.0, 3)
            if p.get("whLifetime") is not None:
                out["pv_lifetime"] = round(p["whLifetime"] / 1000.0, 3)
        elif t == "inverters":
            if p.get("activeCount") is not None:
                out["microinverters_online"] = p["activeCount"]
    for c in js.get("consumption", []) or []:
        if c.get("measurementType") == "total-consumption":
            if c.get("whToday") is not None:
                out["consumption_today"] = round(c["whToday"] / 1000.0, 3)
            if c.get("whLifetime") is not None:
                out["consumption_lifetime"] = round(c["whLifetime"] / 1000.0, 3)
    return out


def parse_meters(readings):
    """Grid voltage + frequency from the first live CT in /ivp/meters/readings."""
    out = {}
    if not isinstance(readings, list):
        return out
    ref = None
    for r in readings:
        if (r.get("voltage") or 0) > 100:   # a live CT on the 230/240 V mains
            ref = r
            break
    if ref:
        if ref.get("voltage") is not None:
            out["grid_voltage"] = round(ref["voltage"], 1)
        if ref.get("freq") is not None:
            out["grid_frequency"] = round(ref["freq"], 2)
    return out


# =============================================================================
# Gateway client — token lifecycle + authenticated local HTTP
# =============================================================================
class EnphaseClient:
    def __init__(self, config):
        self.config = config
        self.host = config["envoy_host"]
        self.serial = (config.get("envoy_serial") or "").strip()
        self.email = (config.get("enlighten_email") or "").strip()
        self.password = config.get("enlighten_password") or ""
        self.manual_token = (config.get("manual_token") or "").strip()
        self.token_file = Path(config.get("token_dir", "/data")) / "enphase_token.jwt"
        self.token = None
        self.sw_version = None
        self.model = "IQ Gateway"

    def base_url(self):
        return f"https://{self.host}"

    # ---- token handling -----------------------------------------------------
    def _cache_token(self, tok):
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(tok.strip())
        except OSError as e:
            logging.warning("Could not cache token: %s", e)

    @staticmethod
    def _valid(tok, margin=7 * 86400):
        if not tok:
            return False
        exp = jwt_exp(tok)
        return exp > 0 and (exp - time.time()) > margin

    def detect_info(self):
        """Read /info.xml (no auth) for serial + software version."""
        try:
            status, body = http_request("GET", f"{self.base_url()}/info.xml",
                                        context=NOVERIFY, timeout=10)
            if status == 200:
                sn = re.search(r"<sn>([^<]+)</sn>", body)
                sw = re.search(r"<software>([^<]+)</software>", body)
                pn = re.search(r"<pn>([^<]+)</pn>", body)
                if sn and not self.serial:
                    self.serial = sn.group(1).strip()
                if sw:
                    self.sw_version = sw.group(1).strip()
                if pn:
                    self.model = f"IQ Gateway ({pn.group(1).strip()})"
                logging.info("Gateway info: serial=%s software=%s", self.serial, self.sw_version)
        except Exception as e:
            logging.warning("Could not read /info.xml: %s", e)

    def _mint_cloud(self):
        """Mint a fresh local token via Enlighten login -> Entrez."""
        if not (self.email and self.password):
            raise RuntimeError("no Enlighten credentials to mint a token")
        if not self.serial:
            self.detect_info()
        if not self.serial:
            raise RuntimeError("gateway serial unknown; set envoy_serial")
        logging.info("Minting token from Enphase cloud (serial %s)", self.serial)
        status, body = http_request("POST", LOGIN_URL,
                                    data={"user[email]": self.email,
                                          "user[password]": self.password}, timeout=30)
        if status != 200:
            raise RuntimeError(f"Enlighten login failed ({status})")
        session_id = json.loads(body).get("session_id")
        if not session_id:
            raise RuntimeError("Enlighten login returned no session_id (check credentials/MFA)")
        status, body = http_request("POST", TOKENS_URL, is_json=True,
                                    data={"session_id": session_id,
                                          "serial_num": self.serial,
                                          "username": self.email}, timeout=30)
        if status != 200 or not body.strip():
            raise RuntimeError(f"token request failed ({status})")
        tok = body.strip()
        if jwt_exp(tok) == 0:
            raise RuntimeError("minted token is not a valid JWT")
        return tok

    def ensure_token(self, force=False):
        """Return a usable token, refreshing if forced/missing/near-expiry."""
        if not force and self._valid(self.token):
            return self.token
        if not force and self.token_file.exists():
            t = self.token_file.read_text().strip()
            if self._valid(t):
                self.token = t
                return t
        if not force and self.manual_token and self._valid(self.manual_token):
            self.token = self.manual_token
            self._cache_token(self.manual_token)
            return self.token
        # Need to mint a fresh one.
        if self.email and self.password:
            try:
                t = self._mint_cloud()
                self.token = t
                self._cache_token(t)
                return t
            except Exception as e:
                logging.error("Token mint failed: %s", e)
        # Last resort: use whatever we have, even if near/after expiry.
        if self.manual_token:
            self.token = self.manual_token
            return self.token
        if self.token_file.exists():
            t = self.token_file.read_text().strip()
            if t:
                self.token = t
                return t
        raise RuntimeError("no usable token and no way to mint one "
                           "(set enlighten_email + enlighten_password, or manual_token)")

    # ---- authenticated local calls -----------------------------------------
    def api(self, path, method="GET", payload=None):
        """Gateway call with one token-refresh retry on 401. Returns (status, body)."""
        url = f"{self.base_url()}{path}"
        result = (0, "")
        for attempt in (1, 2):
            tok = self.ensure_token(force=(attempt == 2))
            status, body = http_request(method, url,
                                        headers={"Authorization": f"Bearer {tok}"},
                                        data=payload, is_json=payload is not None,
                                        context=NOVERIFY, timeout=15)
            result = (status, body)
            if status == 401 and attempt == 1:
                logging.warning("401 from %s — refreshing token", path)
                continue
            return result
        return result

    def api_json(self, path, method="GET", payload=None):
        status, body = self.api(path, method, payload)
        if status != 200:
            raise EnvoyError(f"{path} -> HTTP {status}")
        try:
            return json.loads(body)
        except ValueError:
            raise EnvoyError(f"{path} -> non-JSON response")

    def enable_livedata(self):
        try:
            status, body = self.api("/ivp/livedata/stream", method="POST", payload={"enable": 1})
            logging.info("Livedata stream enable -> %s %s", status, body.strip()[:80])
        except Exception as e:
            logging.warning("Could not enable livedata stream: %s", e)


# =============================================================================
# MQTT publisher (read-only — discovery + state)
# =============================================================================
class MQTTPublisher:
    def __init__(self, config, device):
        self.config = config
        self.base = config["mqtt_base_topic"]
        self.status_topic = f"{self.base}/status"
        self.device = device
        self.client = None
        self.connected = False
        self.expire_after = max(120, int(config["poll_interval"]) * 6)
        if HAS_MQTT and config.get("mqtt_host"):
            self._setup()
        elif not config.get("mqtt_host"):
            logging.warning("No MQTT host configured — running without publishing")

    def _setup(self):
        self.client = mqtt.Client(client_id="enphase_envoy_local", protocol=mqtt.MQTTv311)
        if self.config.get("mqtt_user"):
            self.client.username_pw_set(self.config["mqtt_user"], self.config.get("mqtt_pass", ""))
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.will_set(self.status_topic, payload="offline", qos=1, retain=True)
        try:
            self.client.connect(self.config["mqtt_host"], int(self.config["mqtt_port"]), keepalive=60)
            self.client.loop_start()
        except Exception as e:
            logging.error("MQTT connection failed: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        if rc == 0:
            logging.info("MQTT connected")
        else:
            logging.error("MQTT connect failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logging.warning("MQTT disconnected unexpectedly: rc=%s", rc)

    def publish(self, topic, payload, retain=True):
        if self.client and self.connected:
            self.client.publish(topic, payload, retain=retain)

    def publish_status(self, online):
        self.publish(self.status_topic, "online" if online else "offline")

    def send_discovery(self):
        self._disc_status()
        for s in SENSORS:
            self._disc_sensor(s)

    def _disc_status(self):
        payload = {
            "name": "Gateway Status",
            "unique_id": "enphase_envoy_gateway_status",
            "state_topic": self.status_topic,
            "payload_on": "online", "payload_off": "offline",
            "device_class": "connectivity", "entity_category": "diagnostic",
            "device": self.device,
        }
        self.publish("homeassistant/binary_sensor/enphase_envoy/gateway_status/config",
                     json.dumps(payload))

    def _disc_sensor(self, s):
        oid = s["oid"]
        payload = {
            "name": s["name"],
            "unique_id": f"enphase_envoy_{oid}",
            "state_topic": f"{self.base}/{oid}/state",
            "expire_after": self.expire_after,
            "availability": [{"topic": self.status_topic}],
            "device": self.device,
        }
        if s.get("unit"):
            payload["unit_of_measurement"] = s["unit"]
        if s.get("dclass"):
            payload["device_class"] = s["dclass"]
        if s.get("sclass"):
            payload["state_class"] = s["sclass"]
        if s.get("icon"):
            payload["icon"] = s["icon"]
        self.publish(f"homeassistant/sensor/enphase_envoy/{oid}/config", json.dumps(payload))

    def stop(self):
        if self.client:
            self.publish_status(False)
            self.client.loop_stop()
            self.client.disconnect()


# =============================================================================
# Controller — owns the poll loop
# =============================================================================
class EnphaseController:
    def __init__(self, config):
        self.config = config
        self.running = False
        self.client = EnphaseClient(config)
        self.mqtt = None
        self.discovery_sent = False
        self._poll_count = 0

    def _device(self):
        dev = {
            "identifiers": ["enphase_envoy_local"],
            "name": "Enphase IQ Gateway",
            "manufacturer": "Enphase",
            "model": self.client.model,
            "sw_version": self.client.sw_version or VERSION,
        }
        if self.client.serial:
            dev["serial_number"] = self.client.serial
        return dev

    def setup(self):
        self.client.detect_info()
        self.client.ensure_token()
        self.mqtt = MQTTPublisher(self.config, self._device())
        self.client.enable_livedata()

    def poll_once(self):
        values = {}
        try:
            ld = self.client.api_json("/ivp/livedata/status")
            if ((ld.get("connection") or {}).get("sc_stream")) != "enabled":
                self.client.enable_livedata()
            values.update(parse_livedata(ld))
        except EnvoyError as e:
            logging.warning("livedata: %s", e)
        try:
            values.update(parse_production(self.client.api_json("/production.json")))
        except EnvoyError as e:
            logging.warning("production.json: %s", e)
        try:
            values.update(parse_meters(self.client.api_json("/ivp/meters/readings")))
        except EnvoyError as e:
            logging.warning("meters/readings: %s", e)
        return values

    def run(self):
        self.running = True
        logging.info("=" * 62)
        logging.info("ENPHASE IQ GATEWAY LOCAL v%s", VERSION)
        logging.info("  Gateway: %s (serial %s)", self.config["envoy_host"],
                     self.config.get("envoy_serial") or "auto")
        logging.info("  Poll %ss | MQTT %s", self.config["poll_interval"],
                     "on" if HAS_MQTT else "off")
        logging.info("=" * 62)

        while self.running:
            try:
                self.setup()
                break
            except Exception as e:
                logging.error("Setup failed: %s — retry in 15s", e)
                time.sleep(15)

        fail = 0
        while self.running:
            cycle = time.monotonic()
            try:
                if not self.discovery_sent and self.mqtt and self.mqtt.connected:
                    self.mqtt.send_discovery()
                    self.discovery_sent = True
                    logging.info("MQTT discovery published — entities should appear in HA")

                values = self.poll_once()
                published = 0
                for oid, val in values.items():
                    if self.mqtt:
                        self.mqtt.publish(f"{self.config['mqtt_base_topic']}/{oid}/state", str(val))
                        published += 1
                if self.mqtt:
                    self.mqtt.publish_status(True)
                fail = 0
                self._poll_count += 1
                if self._poll_count == 1 or self._poll_count % 30 == 0:
                    logging.info("Poll #%d: published %d/%d values (MQTT %s)",
                                 self._poll_count, published, len(SENSORS),
                                 "up" if (self.mqtt and self.mqtt.connected) else "DOWN")
            except Exception as e:
                fail += 1
                backoff = min(5 * (2 ** (fail - 1)), 60)
                logging.warning("Poll error: %s — backing off %ss (failure %d)", e, backoff, fail)
                if self.mqtt:
                    self.mqtt.publish_status(False)
                time.sleep(backoff)

            while self.running and (time.monotonic() - cycle) < self.config["poll_interval"]:
                time.sleep(0.2)

    def stop(self):
        self.running = False
        if self.mqtt:
            self.mqtt.stop()

    def probe(self):
        self.client.detect_info()
        self.client.ensure_token()
        self.client.enable_livedata()
        print(f"\nEnphase probe — {self.config['envoy_host']} "
              f"serial {self.client.serial or '?'} sw {self.client.sw_version or '?'}\n")
        values = self.poll_once()
        for s in SENSORS:
            v = values.get(s["oid"])
            unit = s.get("unit", "") if v is not None else ""
            print(f"  {s['name']:<28} {s['oid']:<22} = {v} {unit}")
        print()


# =============================================================================
# Entry point
# =============================================================================
def build_config(args):
    config = DEFAULT_CONFIG.copy()
    env_map = {
        "ENVOY_HOST": ("envoy_host", str),
        "ENVOY_SERIAL": ("envoy_serial", str),
        "ENLIGHTEN_EMAIL": ("enlighten_email", str),
        "ENLIGHTEN_PASSWORD": ("enlighten_password", str),
        "MANUAL_TOKEN": ("manual_token", str),
        "MQTT_HOST": ("mqtt_host", str),
        "MQTT_PORT": ("mqtt_port", int),
        "MQTT_USER": ("mqtt_user", str),
        "MQTT_PASS": ("mqtt_pass", str),
        "MQTT_BASE_TOPIC": ("mqtt_base_topic", str),
        "POLL_INTERVAL": ("poll_interval", int),
        "TOKEN_DIR": ("token_dir", str),
        "LOG_DIR": ("log_dir", str),
    }
    for env_key, (cfg_key, conv) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None and val != "":
            try:
                config[cfg_key] = conv(val)
            except (ValueError, TypeError):
                pass
    if args.host:
        config["envoy_host"] = args.host
    return config


def main():
    parser = argparse.ArgumentParser(description="Enphase IQ Gateway Local poller")
    parser.add_argument("--host", default=None, help="Gateway host/IP override")
    parser.add_argument("--probe", action="store_true", help="Read once, print values, exit")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    config = build_config(args)

    level = logging.DEBUG if (args.debug or os.environ.get("DEBUG") == "true") else logging.INFO
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        Path(config["log_dir"]).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(config["log_dir"]) / "enphase_envoy.log",
            maxBytes=5 * 1024 * 1024, backupCount=3, mode="a"))
    except OSError:
        pass
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)

    controller = EnphaseController(config)

    if args.probe:
        try:
            controller.probe()
        except Exception as e:
            logging.error("Probe failed: %s", e)
            sys.exit(1)
        return

    def shutdown(signum, frame):
        logging.info("Signal %s — shutting down", signum)
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    controller.run()


if __name__ == "__main__":
    main()
