"""Hand-built evidence packs for benchmarking the explainer on cases a small lab never produces.

Each case is (pack, policy profile). They are fiction with realistic shapes, used only
to test wording. Nothing here is measured data.
"""
MI = 2 ** 20


def pack(name, cov=24 * 3600, p95=0.0001, peak=0.001, ws=15 * MI, thr=None, restarts=0,
         oom=False, cpu_req=0.5, cpu_lim=None, mem_req=512 * MI, mem_lim=512 * MI):
    return {
        "schema": "evidence-pack/v1",
        "workload": {"namespace": "synthetic", "name": name, "container": "app", "pods_observed": 1},
        "configured": {"cpu_request_cores": cpu_req, "cpu_limit_cores": cpu_lim,
                       "memory_request_bytes": mem_req, "memory_limit_bytes": mem_lim},
        "observed": {"coverage_seconds": cov, "active_seconds": None,
                     "cpu_cores": {"p50": None if p95 is None else p95 / 2, "p95": p95,
                                   "max": p95, "peak_1m": peak},
                     "memory_bytes": {"max_working_set": ws, "max_usage": ws},
                     "cpu_throttle_ratio": thr, "restarts": restarts, "oom_killed_seen": oom},
        "krr": None,
        "candidate_krr_simple_formula": {
            "cpu_request_cores": p95, "cpu_limit_cores": None,
            "memory_request_bytes": None if ws is None else ws * 1.15,
            "memory_limit_bytes": None if ws is None else ws * 1.15},
        "limitations": [],
    }


CASES = {
    "idle-overprovisioned": (pack("idle-overprovisioned"), "lab"),
    "cpu-throttled": (pack("cpu-throttled", p95=0.036, peak=0.038, ws=40 * MI, thr=0.60,
                           cpu_req=0.1, cpu_lim=0.1, mem_req=64 * MI, mem_lim=64 * MI), "lab"),
    "memory-leak-oom": (pack("memory-leak-oom", ws=124 * MI, restarts=3, oom=True, cpu_req=0.02,
                             mem_req=64 * MI, mem_lim=128 * MI), "lab"),
    "bursty-cpu": (pack("bursty-cpu", p95=0.107, peak=0.613, ws=40 * MI, cpu_req=0.05,
                        mem_req=64 * MI, mem_lim=64 * MI), "lab"),
    "short-history": (pack("short-history", cov=180, p95=0.596, peak=1.0, ws=40 * MI,
                           cpu_req=0.2, mem_req=64 * MI, mem_lim=64 * MI), "lab"),
    "no-usage-data": (pack("no-usage-data", cov=None, p95=None, peak=None, ws=None), "lab"),
    "no-request-set": (pack("no-request-set", p95=0.12, ws=90 * MI, cpu_req=None, mem_req=None,
                            mem_lim=None), "lab"),
    "near-memory-limit": (pack("near-memory-limit", p95=0.05, ws=235 * MI, cpu_req=0.05,
                               mem_req=512 * MI, mem_lim=256 * MI), "lab"),
    "restarts-only": (pack("restarts-only", ws=40 * MI, restarts=3, mem_req=256 * MI,
                           mem_lim=512 * MI), "lab"),
    "peak-blocks-cut": (pack("peak-blocks-cut", p95=0.02, peak=0.9, ws=40 * MI, cpu_req=0.5,
                             mem_req=64 * MI, mem_lim=128 * MI), "lab"),
    "tiny-difference": (pack("tiny-difference", p95=0.45, peak=0.5, ws=420 * MI, cpu_req=0.5,
                             mem_req=512 * MI, mem_lim=1024 * MI), "lab"),
    "production-3-days": (pack("production-3-days", cov=3 * 86400, p95=0.02, ws=30 * MI,
                               cpu_req=1.0, mem_req=1024 * MI, mem_lim=1024 * MI), "production"),
}
