#!/usr/bin/env python3
"""Hourly license beacon for TA-disk (Phase B).

Reads key/telemetry config from local/license.conf (falling back to
default/license.conf), POSTs telemetry to the BentleyPC license API and
indexes exactly one status event per run. Telemetry only - never blocking,
never fails the input, nothing leaves the box without a pasted license key.
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

APP = "TA-IPMI"
LICENSE_APP = "TA-IPMI"
SOURCETYPE = "ta_license:status"
DEFAULT_API = "https://0x9w6cmku4.execute-api.us-east-1.amazonaws.com/production"


def read_conf():
    home = os.environ.get("SPLUNK_HOME") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "..")
    app_dir = os.path.join(home, "etc", "apps", APP)
    if not os.path.isdir(app_dir):
        app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    conf = {"telemetry_enabled": "1", "api_base": DEFAULT_API, "key": ""}
    for layer in ("default", "local"):
        p = os.path.join(app_dir, layer, "license.conf")
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                stanza = None
                for line in fh:
                    line = line.strip()
                    if line.startswith("[") and line.endswith("]"):
                        stanza = line[1:-1].strip().lower()
                    elif "=" in line and stanza in ("settings", None):
                        k, _, v = line.partition("=")
                        k, v = k.strip().lower(), v.split("#")[0].strip().strip("'\"")
                        if k in conf and v:
                            conf[k] = v
        except OSError:
            pass
    version = "0.0.0"
    try:
        with open(os.path.join(app_dir, "default", "app.conf"), encoding="utf-8") as fh:
            for line in fh:
                s = line.strip().lower()
                if s.startswith("version") and "=" in s:
                    version = line.split("=", 1)[1].strip() or version
    except OSError:
        pass
    return conf, version


def emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main():
    conf, version = read_conf()
    key = conf["key"].strip()
    host = socket.gethostname() or os.environ.get("HOSTNAME") or socket.getfqdn()
    fqdn = socket.getfqdn()
    if fqdn and "." in fqdn and not host.endswith("." + fqdn.split(".", 1)[1]):
        host = fqdn
    base = conf["api_base"].rstrip("/")
    if not key:
        emit({"time": int(time.time()), "status": "unconfigured", "app": APP,
              "message": "no license key set - add one on the License page"})
        return 0
    if conf["telemetry_enabled"].strip() not in ("1", "true", "yes"):
        emit({"time": int(time.time()), "status": "disabled", "app": APP,
              "message": "license key set, telemetry off"})
        return 0
    body = json.dumps({"key": key, "host": host, "app": LICENSE_APP.lower(),
                       "version": version}).encode()
    req = urllib.request.Request(base + "/license/beacon", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        if data.get("ok"):
            emit({"time": int(time.time()), "status": "active", "app": APP,
                  "host": host, "version": version, "message": "beacon ok"})
        else:
            emit({"time": int(time.time()), "status": "invalid", "app": APP,
                  "host": host, "reason": str(data.get("reason", "unknown")),
                  "message": "beacon rejected"})
    except Exception as exc:
        emit({"time": int(time.time()), "status": "unreachable", "app": APP,
              "host": host, "reason": type(exc).__name__,
              "message": "beacon transport failed: %s" % str(exc)[:120]})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        emit({"time": int(time.time()), "status": "error", "app": APP,
              "reason": type(exc).__name__})
        sys.exit(0)
