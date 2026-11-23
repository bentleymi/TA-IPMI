import json
import os
import time
import re
import urllib.parse
import urllib.request
import socket

import cherrypy

import splunk.appserver.mrsparkle.controllers as _ctl

APP = "TA-IPMI"
LICENSE_APP = "ta-ipmi"


def _home():
    return os.environ.get("SPLUNK_HOME", "")


def _conf_path():
    return os.path.join(_home(), "etc", "apps", APP, "local", "license.conf")


def _read_settings():
    conf = {"telemetry_enabled": "1", "key": "", "api_base": ""}
    for layer in ("default", "local"):
        p = os.path.join(_home(), "etc", "apps", APP, layer, "license.conf")
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
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
    return conf


def _write_settings(key=None, telemetry=None):
    cur = _read_settings()
    if key is not None:
        if key and not re.fullmatch(r"BP-[A-Za-z0-9_-]{6,64}", key):
            raise ValueError("bad_key_format")
        cur["key"] = key
    if telemetry is not None:
        cur["telemetry_enabled"] = "1" if str(telemetry).lower() in ("1", "true", "on", "yes") else "0"
    d = os.path.dirname(_conf_path())
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(_conf_path(), "w", encoding="utf-8") as fh:
        fh.write("[settings]\n")
        fh.write("telemetry_enabled = %s\n" % cur["telemetry_enabled"])
        fh.write("key = %s\n" % cur["key"])
        if cur.get("api_base"):
            fh.write("api_base = %s\n" % cur["api_base"])
    os.chmod(_conf_path(), 0o600)
    return cur


def _verify(key, base):
    if not key or not base:
        return {"ok": False, "reason": "unconfigured"}
    url = base.rstrip("/") + "/license/verify?key=" + urllib.parse.quote(key) + "&app=" + LICENSE_APP
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:
        return {"ok": False, "reason": "transport:" + type(exc).__name__}


def _status_body():
    conf = _read_settings()
    return {"ok": True,
            "configured": bool(conf["key"]),
            "telemetry_enabled": conf["telemetry_enabled"],
            "verify": _verify(conf["key"], conf["api_base"])}


def _json(body):
    cherrypy.response.headers["Content-Type"] = "application/json"
    return json.dumps(body).encode()


def _index_event(payload):
    try:
        path = "/tmp/ta_license_%s_%d.json" % (APP.replace("-", "_"), int(time.time()))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload) + "\n")
        import splunk.rest as rest
        session = cherrypy.session.get("sessionKey", "")
        rest.simpleRequest(
            "/data/inputs/oneshot?name=%s&index=main&sourcetype=ta_license:status&source=ta_license%3ATA-IPMI&host=%s"
            % (urllib.parse.quote(path), urllib.parse.quote(socket.gethostname())),
            sessionKey=session, method="POST")
    except Exception:
        pass  # status visibility is best-effort, never fail the save


def _immediate_status(conf):
    if not conf["key"]:
        return {"time": int(time.time()), "status": "unconfigured", "app": APP,
                "message": "no license key set - add one on the License page"}
    if conf["telemetry_enabled"].strip() not in ("1", "true", "yes"):
        return {"time": int(time.time()), "status": "disabled", "app": APP,
                "message": "license key set, telemetry off"}
    v = _verify(conf["key"], conf["api_base"])
    if v.get("ok"):
        return {"time": int(time.time()), "status": "active", "app": APP,
                "host": socket.gethostname(), "message": "key saved and verified"}
    return {"time": int(time.time()), "status": "invalid", "app": APP,
            "host": socket.gethostname(), "reason": str(v.get("reason", "unknown")),
            "message": "key saved but rejected: %s" % v.get("reason")}


class LicenseEndpoint(_ctl.BaseController):

    @cherrypy.expose
    def status(self, **kwargs):
        return _json(_status_body())

    @cherrypy.expose
    def save(self, **kwargs):
        try:
            _write_settings(key=kwargs.get("key"), telemetry=kwargs.get("telemetry"))
        except ValueError as exc:
            return _json({"ok": False, "reason": str(exc)})
        _index_event(_immediate_status(_read_settings()))
        return _json(_status_body())


root = LicenseEndpoint
