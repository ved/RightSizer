import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from explainer import collect
from explainer.prom import Prom, PromError

NS = "explainer-lab"
MI = 1024 * 1024


def vec(value, pod, container="app"):
    return {"metric": {"namespace": NS, "pod": pod, "container": container},
            "value": [0, str(value)]}


def serve(rules):
    """Fake Prometheus. rules = [(required substrings, result list)], first match wins."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["query"][0]
            result = []
            for subs, res in rules:
                if all(s in q for s in subs):
                    result = res
                    break
            body = json.dumps({"status": "success",
                               "data": {"resultType": "vector", "result": result}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def rules_for(pod, **over):
    """Canned answers for one pod. Pass name=result-list to override a query."""
    base = {
        "p95": (["quantile_over_time(0.95"], [vec(0.031, pod)]),
        "p50": (["quantile_over_time(0.5,"], [vec(0.012, pod)]),
        "peak1m": (["max_over_time(rate(", "[1m])"], [vec(0.9, pod)]),
        "cpumax": (["max_over_time(rate("], [vec(0.1, pod)]),
        "ws": (["max_over_time(container_memory_working_set"], [vec(20 * MI, pod)]),
        "usage": (["max_over_time(container_memory_usage"], [vec(30 * MI, pod)]),
        "cov": (["count_over_time"], [vec(3600, pod)]),
        "thr": (["cfs_throttled_periods"], [vec(300, pod)]),
        "per": (["cfs_periods_total"], [vec(1000, pod)]),
        "rst": (["restarts_total"], [vec(0, pod)]),
        "oom": (["OOMKilled"], []),
        "cpureq": (["resource_requests", 'resource="cpu"'], [vec(0.1, pod)]),
        "cpulim": (["resource_limits", 'resource="cpu"'], [vec(0.1, pod)]),
        "memreq": (["resource_requests", 'resource="memory"'], [vec(64 * MI, pod)]),
        "memlim": (["resource_limits", 'resource="memory"'], [vec(64 * MI, pod)]),
    }
    for k, res in over.items():
        base[k] = (base[k][0], res)
    return list(base.values())


def run(rules, window="1h"):
    srv, url = serve(rules)
    try:
        data = collect.fetch(Prom(url), NS, window)
        return collect.build_packs(NS, window, data, url)
    finally:
        srv.shutdown()
        srv.server_close()


class WorkloadName(unittest.TestCase):
    def test_names(self):
        w = collect.workload_of
        self.assertEqual(w("review-sample-85d7d6b6c6-79fwc"), "review-sample")
        self.assertEqual(w("failure-sample-7d989c94ff-zv5tv"), "failure-sample")
        self.assertEqual(w("batch-periodic-29412345-abcde"), "batch-periodic")
        self.assertEqual(w("web-0"), "web")
        self.assertEqual(w("lonely"), "lonely")

    def test_window(self):
        self.assertEqual(collect.window_seconds("6h"), 21600)
        self.assertEqual(collect.window_seconds("7d"), 604800)
        with self.assertRaises(ValueError):
            collect.window_seconds("seven days")


class Packs(unittest.TestCase):
    POD = "throttled-cpu-6d5f7c9b8d-abcde"

    def test_basic_pack(self):
        (p,) = run(rules_for(self.POD))
        self.assertEqual(p["workload"]["name"], "throttled-cpu")
        self.assertEqual(p["observed"]["cpu_cores"]["p95"], 0.031)
        self.assertEqual(p["observed"]["cpu_cores"]["peak_1m"], 0.9)
        self.assertEqual(p["observed"]["cpu_cores"]["max"], 0.1)
        self.assertAlmostEqual(p["observed"]["cpu_throttle_ratio"], 0.3)
        self.assertEqual(p["configured"]["memory_limit_bytes"], 64 * MI)
        self.assertFalse(p["observed"]["oom_killed_seen"])
        self.assertAlmostEqual(
            p["candidate_krr_simple_formula"]["memory_request_bytes"], 20 * MI * 1.15)
        self.assertIsNone(p["candidate_krr_simple_formula"]["cpu_limit_cores"])

    def test_missing_is_null_but_zero_stays_zero(self):
        (p,) = run(rules_for(self.POD, cpulim=[], rst=[vec(0, self.POD)]))
        self.assertIsNone(p["configured"]["cpu_limit_cores"])      # unconfigured
        self.assertEqual(p["observed"]["restarts"], 0.0)            # real zero
        self.assertIsNotNone(p["observed"]["restarts"])

    def test_no_limit_means_no_throttle_ratio(self):
        (p,) = run(rules_for(self.POD, thr=[vec(0, self.POD)], per=[vec(0, self.POD)]))
        self.assertIsNone(p["observed"]["cpu_throttle_ratio"])
        self.assertTrue(any("throttling ratio unavailable" in x for x in p["limitations"]))

    def test_nan_becomes_null(self):
        (p,) = run(rules_for(self.POD, p95=[vec("NaN", self.POD)]))
        self.assertIsNone(p["observed"]["cpu_cores"]["p95"])
        self.assertIsNone(p["candidate_krr_simple_formula"]["cpu_request_cores"])

    def test_oom_detected(self):
        (p,) = run(rules_for(self.POD, oom=[vec(1, self.POD)]))
        self.assertTrue(p["observed"]["oom_killed_seen"])

    def test_short_history_is_flagged(self):
        (p,) = run(rules_for(self.POD, cov=[vec(600, self.POD)]), window="1h")
        self.assertTrue(any("History covers" in x for x in p["limitations"]))

    def test_pods_of_one_workload_are_merged(self):
        a, b = "app-6d5f7c9b8d-aaaaa", "app-6d5f7c9b8d-bbbbb"
        rules = rules_for(a, p95=[vec(0.2, a), vec(0.5, b)],
                          rst=[vec(1, a), vec(2, b)],
                          ws=[vec(10 * MI, a), vec(40 * MI, b)])
        (p,) = run(rules)
        self.assertEqual(p["workload"]["pods_observed"], 2)
        self.assertEqual(p["observed"]["cpu_cores"]["p95"], 0.5)     # max across pods
        self.assertEqual(p["observed"]["restarts"], 3.0)             # summed
        self.assertEqual(p["observed"]["memory_bytes"]["max_working_set"], 40 * MI)


class Cli(unittest.TestCase):
    def test_writes_files_and_summary(self):
        pod = "throttled-cpu-6d5f7c9b8d-abcde"
        srv, url = serve(rules_for(pod))
        try:
            with tempfile.TemporaryDirectory() as d:
                rc = collect.main(["--prom", url, "-n", NS, "--window", "1h", "--out", d])
                self.assertEqual(rc, 0)
                f = Path(d) / NS / "throttled-cpu__app.json"
                self.assertEqual(json.loads(f.read_text())["schema"], collect.SCHEMA)
        finally:
            srv.shutdown()
            srv.server_close()
        srv.server_close()

    def test_unreachable_prometheus(self):
        self.assertEqual(collect.main(["--prom", "http://127.0.0.1:1", "-n", NS]), 2)

    def test_bad_window(self):
        self.assertEqual(collect.main(["-n", NS, "--window", "soon"]), 2)

    def test_client_raises(self):
        with self.assertRaises(PromError):
            Prom("http://127.0.0.1:1", timeout=1).query("up")


if __name__ == "__main__":
    unittest.main()
