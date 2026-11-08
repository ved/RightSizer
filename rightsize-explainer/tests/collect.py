"""Collect one evidence pack per workload container from Prometheus.

An evidence pack is a JSON file holding every number the explainer is allowed to
use. Missing data is stored as null, never as 0, so "no data" and "zero" stay
different things.
"""
import argparse
import json
import math
import pathlib
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from .prom import Prom, PromError

SCHEMA = "evidence-pack/v1"
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MEMORY_BUFFER = 1.15  # KRR "simple" default: maximum working set + 15%


def window_seconds(window):
    m = re.fullmatch(r"(\d+)([smhdw])", window)
    if not m:
        raise ValueError(f"bad window {window!r}; use a form like 6h or 7d")
    return int(m.group(1)) * _UNITS[m.group(2)]


# Workload name is inferred from the pod name. A later step should replace this
# with a join on kube_pod_owner.
_CRON = re.compile(r"^(.+)-\d{7,10}-[a-z0-9]{5}$")
_DEPLOY = re.compile(r"^(.+)-[a-z0-9]{9,10}-[a-z0-9]{5}$")
_STS = re.compile(r"^(.+)-\d+$")


def workload_of(pod):
    for rx in (_CRON, _DEPLOY, _STS):
        m = rx.match(pod)
        if m:
            return m.group(1)
    return pod


def queries(ns, w):
    s = f'namespace="{ns}",container!="",container!="POD"'
    cpu_rate = f"rate(container_cpu_usage_seconds_total{{{s}}}[5m])"
    return {
        "cpu_p50": f"quantile_over_time(0.5, {cpu_rate}[{w}:1m])",
        "cpu_p95": f"quantile_over_time(0.95, {cpu_rate}[{w}:1m])",
        "cpu_max": f"max_over_time({cpu_rate}[{w}:1m])",
        # 1-minute rate on a 30 s step: catches short bursts that a 5-minute rate smooths away
        "cpu_peak_1m": (
            f"max_over_time(rate(container_cpu_usage_seconds_total{{{s}}}[1m])[{w}:30s])"
        ),
        "mem_max_working_set": f"max_over_time(container_memory_working_set_bytes{{{s}}}[{w}])",
        "mem_max_usage": f"max_over_time(container_memory_usage_bytes{{{s}}}[{w}])",
        "coverage_s": f"count_over_time(container_memory_working_set_bytes{{{s}}}[{w}:1m]) * 60",
        "throttled_periods": f"increase(container_cpu_cfs_throttled_periods_total{{{s}}}[{w}])",
        "cfs_periods": f"increase(container_cpu_cfs_periods_total{{{s}}}[{w}])",
        "restarts": f"increase(kube_pod_container_status_restarts_total{{{s}}}[{w}])",
        "oom_seen": (
            f'max_over_time(kube_pod_container_status_last_terminated_reason'
            f'{{{s},reason="OOMKilled"}}[{w}])'
        ),
        "cpu_request": f'kube_pod_container_resource_requests{{{s},resource="cpu"}}',
        "cpu_limit": f'kube_pod_container_resource_limits{{{s},resource="cpu"}}',
        "mem_request": f'kube_pod_container_resource_requests{{{s},resource="memory"}}',
        "mem_limit": f'kube_pod_container_resource_limits{{{s},resource="memory"}}',
    }


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def fetch(prom, ns, window):
    """Run every query; return {name: {(pod, container): value-or-None}}."""
    out = {}
    for name, q in queries(ns, window).items():
        rows = {}
        for r in prom.query(q):
            m = r["metric"]
            rows[(m.get("pod"), m.get("container"))] = _num(r["value"][1])
        out[name] = rows
    return out


def _vals(data, name, keys):
    return [data[name][k] for k in keys if data[name].get(k) is not None]


def _max(data, name, keys):
    v = _vals(data, name, keys)
    return max(v) if v else None


def _sum(data, name, keys):
    v = _vals(data, name, keys)
    return sum(v) if v else None


def build_packs(ns, window, data, prom_url, now=None):
    """Turn raw per-pod numbers into one pack per (workload, container)."""
    now = now or datetime.now(timezone.utc)
    all_keys = {k for rows in data.values() for k in rows if k[0] and k[1]}
    groups = defaultdict(list)
    for pod, container in all_keys:
        groups[(workload_of(pod), container)].append((pod, container))

    wanted = window_seconds(window)
    packs = []
    for (workload, container), keys in sorted(groups.items()):
        coverage = _max(data, "coverage_s", keys)
        throttled = _sum(data, "throttled_periods", keys)
        periods = _sum(data, "cfs_periods", keys)
        ratio = throttled / periods if throttled is not None and periods else None
        cpu_p95 = _max(data, "cpu_p95", keys)
        mem_max = _max(data, "mem_max_working_set", keys)

        limitations = [
            "Workload name is inferred from the pod name.",
            "Revisions are not tracked, so samples from different releases are mixed.",
            "Usage figures are the highest value across pods (conservative).",
            "p50/p95/max use a 5-minute CPU rate; peak_1m uses a 1-minute rate and is "
            "still limited by the scrape interval.",
            "No latency, error-rate or SLO data is included.",
        ]
        if coverage is None:
            limitations.append("No usage samples were found in this window.")
        elif coverage < wanted * 0.9:
            limitations.append(
                f"History covers about {coverage / 3600:.1f} h of the requested "
                f"{wanted / 3600:.1f} h window."
            )
        if ratio is None:
            limitations.append(
                "CPU throttling ratio unavailable: no CPU limit is enforced, or the "
                "cAdvisor throttling metrics are missing."
            )

        packs.append({
            "schema": SCHEMA,
            "generated_at": now.isoformat(timespec="seconds"),
            "source": {"prometheus": prom_url, "window": window},
            "workload": {
                "namespace": ns,
                "name": workload,
                "container": container,
                "pods_observed": len({k[0] for k in keys}),
            },
            "configured": {
                "cpu_request_cores": _max(data, "cpu_request", keys),
                "cpu_limit_cores": _max(data, "cpu_limit", keys),
                "memory_request_bytes": _max(data, "mem_request", keys),
                "memory_limit_bytes": _max(data, "mem_limit", keys),
            },
            "observed": {
                "coverage_seconds": coverage,
                "cpu_cores": {
                    "p50": _max(data, "cpu_p50", keys),
                    "p95": cpu_p95,
                    "max": _max(data, "cpu_max", keys),
                    "peak_1m": _max(data, "cpu_peak_1m", keys),
                },
                "memory_bytes": {
                    "max_working_set": mem_max,
                    "max_usage": _max(data, "mem_max_usage", keys),
                },
                "cpu_throttle_ratio": ratio,
                "restarts": _sum(data, "restarts", keys),
                "oom_killed_seen": any(v > 0 for v in _vals(data, "oom_seen", keys)),
            },
            # Reproduces KRR's default "simple" arithmetic. It is NOT the output of
            # running KRR; a later step replaces it with real KRR and VPA output.
            "candidate_krr_simple_formula": {
                "cpu_request_cores": cpu_p95,
                "cpu_limit_cores": None,
                "memory_request_bytes": mem_max * MEMORY_BUFFER if mem_max is not None else None,
                "memory_limit_bytes": mem_max * MEMORY_BUFFER if mem_max is not None else None,
            },
            "limitations": limitations,
        })
    return packs


def write_packs(packs, out_dir):
    paths = []
    for p in packs:
        w = p["workload"]
        d = pathlib.Path(out_dir) / w["namespace"]
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{w['name']}__{w['container']}.json"
        path.write_text(json.dumps(p, indent=2) + "\n")
        paths.append(path)
    return paths


def _fmt(v, scale=1.0, unit="", digits=2):
    return "-" if v is None else f"{v / scale:.{digits}f}{unit}"


def summary(packs):
    head = f"{'workload':<26}{'hist(h)':>8}{'cpu p95':>9}{'cpu req':>9}{'pk 1m':>8}{'mem max':>10}{'mem req':>10}{'throttle':>9}{'restarts':>9}{'oom':>5}"
    lines = [head, "-" * len(head)]
    for p in packs:
        o, c = p["observed"], p["configured"]
        lines.append(
            f"{p['workload']['name'] + '/' + p['workload']['container']:<26}"
            f"{_fmt(o['coverage_seconds'], 3600, '', 1):>8}"
            f"{_fmt(o['cpu_cores']['p95'], 1, '', 3):>9}"
            f"{_fmt(c['cpu_request_cores'], 1, '', 3):>9}"
            f"{_fmt(o['cpu_cores']['peak_1m'], 1, '', 3):>8}"
            f"{_fmt(o['memory_bytes']['max_working_set'], 2**20, 'Mi', 0):>10}"
            f"{_fmt(c['memory_request_bytes'], 2**20, 'Mi', 0):>10}"
            f"{_fmt(None if o['cpu_throttle_ratio'] is None else o['cpu_throttle_ratio'] * 100, 1, '%', 0):>9}"
            f"{_fmt(o['restarts'], 1, '', 0):>9}"
            f"{'yes' if o['oom_killed_seen'] else 'no':>5}"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Collect evidence packs from Prometheus.")
    ap.add_argument("--prom", default="http://127.0.0.1:9090", help="Prometheus URL")
    ap.add_argument("--namespace", "-n", action="append", required=True,
                    help="namespace to collect (repeatable)")
    ap.add_argument("--window", default="6h", help="history window, e.g. 6h, 7d (default 6h)")
    ap.add_argument("--out", default="evidence", help="output directory (default ./evidence)")
    args = ap.parse_args(argv)
    try:
        window_seconds(args.window)
        prom = Prom(args.prom)
        total = []
        for ns in args.namespace:
            data = fetch(prom, ns, args.window)
            packs = build_packs(ns, args.window, data, args.prom)
            if not packs:
                print(f"warning: no containers found in namespace {ns!r}", file=sys.stderr)
            write_packs(packs, args.out)
            total.extend(packs)
    except (PromError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not total:
        return 1
    print(summary(total))
    print(f"\nWrote {len(total)} evidence pack(s) to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
