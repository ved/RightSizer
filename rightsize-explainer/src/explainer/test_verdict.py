import unittest

from explainer import verdict
from explainer.verdict import MI, PROFILES, evaluate, get

LAB = PROFILES["lab"]


def pack(cov=1440 * 60, p95=0.0001, peak=0.001, ws=15 * MI, thr=None, restarts=0, oom=False,
         cpu_req=0.5, cpu_lim=None, mem_req=512 * MI, mem_lim=512 * MI):
    return {
        "schema": "evidence-pack/v1",
        "workload": {"namespace": "explainer-lab", "name": "w", "container": "app",
                     "pods_observed": 1},
        "configured": {"cpu_request_cores": cpu_req, "cpu_limit_cores": cpu_lim,
                       "memory_request_bytes": mem_req, "memory_limit_bytes": mem_lim},
        "observed": {"coverage_seconds": cov,
                     "cpu_cores": {"p50": None if p95 is None else p95 / 2, "p95": p95, "max": p95, "peak_1m": peak},
                     "memory_bytes": {"max_working_set": ws, "max_usage": ws},
                     "cpu_throttle_ratio": thr, "restarts": restarts, "oom_killed_seen": oom},
        "candidate_krr_simple_formula": {
            "cpu_request_cores": p95, "cpu_limit_cores": None,
            "memory_request_bytes": None if ws is None else ws * 1.15,
            "memory_limit_bytes": None if ws is None else ws * 1.15},
        "limitations": [],
    }


# The five lab scenarios, with the numbers seen in the real collector output.
STEADY = pack()
THROTTLED = pack(p95=0.036, peak=0.038, ws=4 * MI, thr=0.60, cpu_req=0.1, cpu_lim=0.1,
                 mem_req=64 * MI, mem_lim=64 * MI)
LEAKY = pack(p95=0.0001, peak=0.0004, ws=124 * MI, restarts=1, oom=True, cpu_req=0.02,
             mem_req=64 * MI, mem_lim=128 * MI)
BURSTY = pack(p95=0.107, peak=0.613, ws=4 * MI, cpu_req=0.05, mem_req=64 * MI, mem_lim=64 * MI)
BATCH = pack(cov=180, p95=0.596, peak=1.0, ws=4 * MI, cpu_req=0.2, mem_req=64 * MI,
             mem_lim=64 * MI)
SCENARIOS = {"steady": STEADY, "throttled": THROTTLED, "leaky": LEAKY, "bursty": BURSTY,
             "batch": BATCH}


def codes(res):
    return {r["code"] for r in res["reasons"]}


class LabScenarios(unittest.TestCase):
    def rec(self, p):
        v = evaluate(p, LAB)
        return v["cpu"]["recommendation"], v["memory"]["recommendation"], v

    def test_steady_overprovisioned_is_safe_to_cut(self):
        cpu, mem, v = self.rec(STEADY)
        self.assertEqual((cpu, mem), ("decrease_ok", "decrease_ok"))
        self.assertEqual(v["cpu"]["candidate_adjusted"], 0.01)       # floor applied
        self.assertEqual(v["memory"]["candidate_adjusted"], 32 * MI)  # floor applied
        self.assertIn("I_FLOOR", codes(v["cpu"]))

    def test_throttled_cpu_must_not_be_cut(self):
        cpu, mem, v = self.rec(THROTTLED)
        self.assertEqual(cpu, "decrease_blocked")
        self.assertIn("C_THROTTLED", codes(v["cpu"]))
        self.assertEqual(mem, "decrease_ok")

    def test_leaky_memory_needs_more_not_less(self):
        cpu, mem, v = self.rec(LEAKY)
        self.assertEqual(mem, "increase_supported")
        self.assertTrue({"S_OOM", "S_NEAR_LIMIT"} <= codes(v["memory"]))
        self.assertEqual(cpu, "decrease_ok")

    def test_bursty_cpu_increase_with_peak_warning(self):
        cpu, mem, v = self.rec(BURSTY)
        self.assertEqual(cpu, "increase_supported")
        self.assertIn("C_PEAK_ABOVE_CANDIDATE", codes(v["cpu"]))
        warn = [r for r in v["cpu"]["reasons"] if r["code"] == "C_PEAK_ABOVE_CANDIDATE"][0]
        self.assertEqual(warn["severity"], "warn")

    def test_batch_short_history(self):
        cpu, mem, v = self.rec(BATCH)
        self.assertEqual(cpu, "increase_supported")                  # usage above request
        self.assertIn("E_SHORT_HISTORY", codes(v["cpu"]))
        self.assertEqual(mem, "insufficient_evidence")               # decrease, short history


class Rules(unittest.TestCase):
    def test_peak_blocks_a_cpu_decrease(self):
        p = pack(p95=0.05, peak=0.9, cpu_req=0.5)
        v = evaluate(p, LAB)
        self.assertEqual(v["cpu"]["recommendation"], "decrease_blocked")
        self.assertIn("C_PEAK_ABOVE_CANDIDATE", codes(v["cpu"]))

    def test_memory_near_limit_blocks_decrease(self):
        p = pack(ws=60 * MI, mem_req=200 * MI, mem_lim=64 * MI)
        v = evaluate(p, LAB)
        self.assertEqual(v["memory"]["recommendation"], "decrease_blocked")
        self.assertIn("M_NEAR_LIMIT", codes(v["memory"]))

    def test_restarts_block_a_memory_decrease(self):
        v = evaluate(pack(restarts=2), LAB)
        self.assertEqual(v["memory"]["recommendation"], "decrease_blocked")
        self.assertIn("M_RESTARTS", codes(v["memory"]))

    def test_small_change_is_no_change(self):
        v = evaluate(pack(p95=0.5, cpu_req=0.52, peak=0.5), LAB)
        self.assertEqual(v["cpu"]["recommendation"], "no_change")
        self.assertIn("I_IMMATERIAL", codes(v["cpu"]))

    def test_unset_request_has_no_baseline(self):
        v = evaluate(pack(cpu_req=None, mem_req=None, mem_lim=None), LAB)
        self.assertEqual(v["cpu"]["recommendation"], "no_request_set")
        self.assertEqual(v["memory"]["recommendation"], "no_request_set")

    def test_no_data_is_insufficient(self):
        v = evaluate(pack(cov=None, p95=None, ws=None), LAB)
        self.assertEqual(v["cpu"]["recommendation"], "insufficient_evidence")
        self.assertIn("E_NO_DATA", codes(v["cpu"]))

    def test_production_profile_demands_long_history(self):
        v = evaluate(STEADY, PROFILES["production"])                # one day of history
        self.assertEqual(v["cpu"]["recommendation"], "insufficient_evidence")
        self.assertEqual(v["memory"]["recommendation"], "insufficient_evidence")

    def test_unknown_throttling_with_a_limit_is_flagged(self):
        v = evaluate(pack(p95=0.05, peak=0.06, cpu_req=0.5, cpu_lim=1.0, thr=None), LAB)
        self.assertIn("C_THROTTLE_UNKNOWN", codes(v["cpu"]))


class Evidence(unittest.TestCase):
    def test_every_cited_field_exists_and_matches_the_pack(self):
        for name, p in SCENARIOS.items():
            v = evaluate(p, LAB)
            for res in ("cpu", "memory"):
                for r in v[res]["reasons"]:
                    self.assertTrue(r["evidence"], f"{name}/{res}/{r['code']} cites nothing")
                    for path, val in r["evidence"].items():
                        self.assertEqual(get(p, path), val, f"{name}/{res}/{r['code']}/{path}")

    def test_every_scenario_gets_a_recommendation(self):
        valid = {"no_change", "no_request_set", "decrease_ok", "decrease_blocked",
                 "increase_supported", "increase_optional", "insufficient_evidence"}
        for p in SCENARIOS.values():
            v = evaluate(p, LAB)
            self.assertIn(v["cpu"]["recommendation"], valid)
            self.assertIn(v["memory"]["recommendation"], valid)


if __name__ == "__main__":
    unittest.main()
