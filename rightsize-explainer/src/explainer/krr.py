"""Read the JSON that `krr simple -f json` writes.

KRR's own recommendation becomes a separate block in the evidence pack, so the
verdict rules can use the real tool's number instead of a re-implemented formula.

Things this adapter is careful about:
  * KRR reports "?" when it has too little data. That becomes None, with KRR's
    own explanation kept in `info`.
  * KRR applies its own minimums (--cpu-min, --mem-min). A recommendation that
    equals the minimum is not usage-based, so it is flagged `floor_applied`.
  * KRR's --mem-min is documented in "MB" but its output equals MiB * 2**20.
"""
import json
import math

MIB = 2 ** 20


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _at(value, floor):
    return value is not None and math.isclose(value, floor, rel_tol=1e-6)


def load(path):
    """Return {(namespace, name, container): parsed KRR entry}."""
    with open(path) as f:
        data = json.load(f)
    settings = data.get("strategy", {}).get("settings", {})
    cfg = data.get("config", {})
    floors = {"cpu_min_cores": cfg.get("cpu_min_value", 0) / 1000,
              "mem_min_bytes": cfg.get("memory_min_value", 0) * MIB}
    out = {}
    for scan in data.get("scans", []):
        o, rec = scan["object"], scan["recommended"]
        cpu = _num(rec["requests"]["cpu"]["value"])
        mem = _num(rec["requests"]["memory"]["value"])
        info = rec.get("info") or {}
        out[(o["namespace"], o["name"], o["container"])] = {
            "source": "krr",
            "strategy": data.get("strategy", {}).get("name"),
            "kind": o.get("kind"),
            "settings": {k: settings.get(k) for k in (
                "history_duration", "timeframe_duration", "cpu_percentile",
                "memory_buffer_percentage", "points_required")},
            "floors": floors,
            "recommended": {
                "cpu_request_cores": cpu,
                "memory_request_bytes": mem,
                "memory_limit_bytes": _num(rec["limits"]["memory"]["value"]),
            },
            "floor_applied": {"cpu": _at(cpu, floors["cpu_min_cores"]),
                              "memory": _at(mem, floors["mem_min_bytes"])},
            "info": {"cpu": info.get("cpu"), "memory": info.get("memory")},
            "severity": scan.get("severity"),
        }
    return out
