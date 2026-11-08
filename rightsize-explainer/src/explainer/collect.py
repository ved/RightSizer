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
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import krr as krr_mod
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
    by = "namespace,pod,container"
    # A restarted container gets a new series under the same pod and container name.
    # Rates and coverage are merged first ("max by") so the two incarnations form one
    # continuous series. Range-vector queries are merged later in fetch().
    cpu5 = f"max by ({by}) (rate(container_cpu_usage_seconds_total{{{s}}}[5m]))"
    cpu1 = f"max by ({by}) (rate(container_cpu_usage_seconds_total{{{s}}}[1m]))"
    return {
        "cpu_p50": f"quantile_over_time(0.5, {cpu5}[{w}:1m])",
        "cpu_p95": f"quantile_over_time(0.95, {cpu5}[{w}:1m])",
        "cpu_max": f"max_over_time({cpu5}[{w}:1m])",
        # 1-minute rate on a 30 s step: catches short bursts that a 5-minute rate smooths away
        "cpu_peak_1m": f"max_over_time({cpu1}[{w}:30s])",
        "mem_max_working_set": f"max_over_time(container_memory_working_set_bytes{{{s}}}[{w}])",
        "mem_max_usage": f"max_over_time(container_memory_usage_bytes{{{s}}}[{w}])",
        "coverage_s": (
            f"count_over_time(max by ({by}) (container_memory_working_set_bytes{{{s}}})"
            f"[{w}:1m]) * 60"
        ),
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


# Counter increases from different container incarnations add up; everything else takes the max.
_ADDITIVE = {"throttled_periods", "cfs_periods", "restarts"}


def fetch(prom, ns, window):
    """Run every query; return {name: {(pod, container): value-or-None}}."""
    out = {}
    for name, q in queries(ns, window).items():
        rows = {}
        for r in prom.query(q):
            m = r["metric"]
            key = (m.get("pod"), m.get("container"))
            val = _num(r["value"][1])
            if key in rows and rows[key] is not None and val is not None:
                val = rows[key] + val if name in _ADDITIVE else max(rows[key], val)
            elif key in rows and val is None:
                val = rows[key]
            rows[key] = val
        out[name] = rows
    return out


def workload_history(prom, ns, window, now=None):
    """Per (workload, container): how long it has been observed.

    Returns {key: {"span": seconds from first to last sample, "active": seconds with samples}}.
    Done per workload, not per pod, so a CronJob whose pods live 4 minutes each still
    shows the hours over which its runs were seen.
    """
    now = now or time.time()
    w = window_seconds(window)
    step = 60 if w / 60 <= 10000 else int(math.ceil(w / 10000 / 60)) * 60
    s = f'namespace="{ns}",container!="",container!="POD"'
    q = f"max by (namespace,pod,container) (container_memory_working_set_bytes{{{s}}})"
    buckets = defaultdict(set)
    for r in prom.query_range(q, now - w, now, step):
        m = r["metric"]
        if not (m.get("pod") and m.get("container")):
            continue
        key = (workload_of(m["pod"]), m["container"])
        for ts, _ in r.get("values", []):
            buckets[key].add(int(float(ts)) // step)
    return {k: {"span": (max(b) - min(b) + 1) * step, "active": len(b) * step}
            for k, b in buckets.items()}


def _whole(v):
    """increase() extrapolates (1.01 restarts); counts of events are whole numbers."""
    return None if v is None else int(round(v))


def _vals(data, name, keys):
    return [data[name][k] for k in keys if data[name].get(k) is not None]


def _max(data, name, keys):
    v = _vals(data, name, keys)
    return max(v) if v else None


def _sum(data, name, keys):
    v = _vals(data, name, keys)
    return sum(v) if v else None


def build_packs(ns, window, data, prom_url, now=None, history=None, krr=None):
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
        hist = (history or {}).get((workload, container))
        active = None
        if hist:
            coverage, active = hist["span"], hist["active"]
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
        if active is not None and coverage and active < coverage * 0.8:
            limitations.append(
                f"Workload runs intermittently: {active / 3600:.1f} h of samples across a "
                f"{coverage / 3600:.1f} h span; percentiles describe only the time it was running."
            )
        krr_entry = None
        if krr is not None:
            krr_entry = krr.get((ns, workload, container))
            if krr_entry is None:
                limitations.append("The KRR output has no entry for this workload.")
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
                "active_seconds": active,
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
                "restarts": _whole(_sum(data, "restarts", keys)),
                "oom_killed_seen": any(v > 0 for v in _vals(data, "oom_seen", keys)),
            },
            # KRR's own recommendation, read from `krr simple -f json`; null when no file was given.
            "krr": krr_entry,
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
    ap.add_argument("--krr", help="JSON written by `krr simple -f json`; adds KRR's recommendation")
    args = ap.parse_args(argv)
    try:
        window_seconds(args.window)
        prom = Prom(args.prom)
        krr = krr_mod.load(args.krr) if args.krr else None
        total = []
        for ns in args.namespace:
            data = fetch(prom, ns, args.window)
            packs = build_packs(ns, args.window, data, args.prom,
                                history=workload_history(prom, ns, args.window), krr=krr)
            if not packs:
                print(f"warning: no containers found in namespace {ns!r}", file=sys.stderr)
            write_packs(packs, args.out)
            total.extend(packs)
    except (PromError, ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not total:
        return 1
    print(summary(total))
    print(f"\nWrote {len(total)} evidence pack(s) to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
