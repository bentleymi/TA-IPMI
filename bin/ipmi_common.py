#!/usr/bin/env python3
"""Shared collection logic for TA-IPMI scripted inputs."""

import json
import os
import re
import socket
import subprocess
import sys

IPMI_TOOL = os.environ.get("IPMI_TOOL", "ipmitool")
DEV_PATHS = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")
NUM_RE = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+))")
STATUSES = {
    "ok", "nc", "cr", "nr", "unr", "ns", "na", "uc", "lc", "lo", "hi",
    "no reading", "not available", "not disclosed", "transition",
}


def is_status(token):
    return re.sub(r"\s+", " ", token.lower()).strip() in STATUSES


def run_ipmi(args):
    proc = subprocess.run(
        [IPMI_TOOL, "-I", "open"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def bmc_present():
    return any(os.path.exists(p) for p in DEV_PATHS)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def numeric(value):
    match = NUM_RE.fullmatch(value.strip())
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def pipe_columns(line):
    return [col.strip() for col in line.split("|")]


def state_dir():
    home = os.environ.get("SPLUNK_HOME", "")
    path = os.path.join(home, "var", "lib", "splunk", "TA-IPMI")
    try:
        os.makedirs(path)
    except OSError:
        pass
    return path


def collect_sensors():
    rc, out, err = run_ipmi(["sdr", "list", "full"])
    if rc != 0:
        rc, out, err = run_ipmi(["sdr", "list"])
    if rc != 0:
        raise RuntimeError("ipmitool sdr list failed (rc=%d): %s" % (rc, err))
    sensors = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        cols = pipe_columns(line)
        if len(cols) < 2:
            continue
        name = cols[0]
        tokens = cols[1].split()
        reading, units, status = cols[1], "", ""
        if len(tokens) == 1 and numeric(tokens[0]) is not None:
            reading, units = tokens[0], cols[2] if len(cols) > 2 else ""
            status = cols[3] if len(cols) > 3 else ""
        elif len(tokens) >= 2 and numeric(tokens[0]) is not None:
            reading, units = tokens[0], " ".join(tokens[1:])
            status = cols[2] if len(cols) > 2 else ""
        if not is_status(status):
            status = ""
            for candidate in cols[2:]:
                if is_status(candidate):
                    status = candidate
                    break
        key = slug(name)
        if not key:
            continue
        sensors[key] = {
            "sensor_name": name,
            "reading": reading,
            "reading_value": numeric(reading),
            "units": units,
            "status": status.lower(),
        }
        if len(cols) > 4 and not is_status(cols[2] if len(cols) > 2 else ""):
            sensors[key]["thresholds"] = cols[4:]
        elif len(cols) > 3 and is_status(status) and status in cols:
            sensors[key]["thresholds"] = cols[cols.index(status) + 1:]
    return sensors


def pipe_kv(out, prefix):
    result = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        cols = pipe_columns(line)
        if len(cols) < 2:
            continue
        key = slug(cols[0])
        if not key:
            continue
        result["%s_%s" % (prefix, key)] = cols[1]
    return result


def collect_status():
    chassis = {}
    rc, out, err = run_ipmi(["chassis", "status"])
    if rc == 0:
        chassis = pipe_kv(out, "chassis")
    else:
        chassis = {"chassis_error": err or ("rc=%d" % rc)}
    mc = {}
    rc, out, err = run_ipmi(["mc", "info"] )
    if rc == 0:
        mc = pipe_kv(out, "bmc")
    mc["bmc_firmware"] = mc.get("bmc_firmware_revision", "")
    chassis.update(mc)
    return chassis


def collect_sel():
    rc, out, err = run_ipmi(["sel", "list"])
    if rc != 0:
        raise RuntimeError("ipmitool sel list failed (rc=%d): %s" % (rc, err))
    records = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 4:
            continue
        try:
            record_id = int(cols[0], 16)
        except ValueError:
            continue
        records.append({
            "sel_record_id": cols[0],
            "sel_date": cols[1] if len(cols) > 1 else "",
            "sel_time": cols[2] if len(cols) > 2 else "",
            "sel_sensor": cols[3] if len(cols) > 3 else "",
            "sel_type": cols[4] if len(cols) > 4 else "",
            "sel_assertion": cols[5] if len(cols) > 5 else "",
            "sel_data": cols[6] if len(cols) > 6 else "",
            "record_id_int": record_id,
            "raw": line,
        })
    return records


def emit_one(event, host):
    event["host"] = host
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")


def main(mode):
    host = socket.gethostname()
    if not bmc_present():
        sys.stderr.write(
            "TA-IPMI: no local BMC interface (missing /dev/ipmi0); "
            "load ipmi_si/ipmi_devintf or disable this input on BMC-less hosts.\n"
        )
        return 0
    try:
        if mode == "sel":
            records = collect_sel()
            high_water = 0
            path = os.path.join(state_dir(), "sel_last_id")
            if os.path.exists(path):
                try:
                    with open(path) as fh:
                        high_water = int((fh.read() or "0").strip() or 0)
                except (ValueError, IOError):
                    high_water = 0
            new = [r for r in records if r["record_id_int"] > high_water]
            for record in new:
                event = dict(record)
                event.pop("record_id_int", None)
                emit_one(event, host)
            sys.stdout.flush()
            if new:
                try:
                    with open(path, "w") as fh:
                        fh.write(str(max(r["record_id_int"] for r in new)))
                except IOError:
                    pass
            return 0
        sensors = collect_sensors()
    except Exception as exc:
        sys.stderr.write("TA-IPMI collection error: %s\n" % exc)
        return 2
    if mode == "metrics":
        chassis = collect_status()
        event = {}
        for key, value in chassis.items():
            number = numeric(value)
            if number is None:
                continue
            event[key] = number
        for key in list(sensors):
            data = sensors[key]
            if data["reading_value"] is None:
                continue
            event[key] = data["reading_value"]
            event[key + "_status"] = data["status"]
        event["sensor_count"] = len(sensors)
        emit_one(event, host)
        sys.stdout.flush()
    else:
        chassis = collect_status()
        event = dict(chassis)
        event["sensors"] = sensors
        emit_one(event, host)
        sys.stdout.flush()
    return 0
