"""KRR adapter tests. The fixture is trimmed from a real `krr simple -f json` run on the lab."""
import copy
import unittest
from pathlib import Path

from explainer import collect, krr, verdict
from explainer.verdict import MI, PROFILES, evaluate, get
from test_collect import NS, rules_for, serve
from test_verdict import BATCH, BURSTY, LEAKY, STEADY, THROTTLED, codes

from explainer.prom import Prom

FIXTURE = Path(__file__).parent / "fixtures" / "krr-lab.json"
LAB = PROFILES["lab"]


def entry(name, container="app"):
    return krr.load(FIXTURE)[(NS, name, container)]


def with_krr(pack, name, container="app"):
    p = copy.deepcopy(pack)
    p["krr"] = entry(name, container)
    return p


class Load(unittest.TestCase):
    def test_parses_all_five(self):
        self.assertEqual(len(krr.load(FIXTURE)), 5)

    def test_usage_based_value_is_kept(self):
        e = entry("leaky-memory")
        self.assertEqual(e["recommended"]["memory_request_bytes"], 149946368.0)
        self.assertFalse(e["floor_applied"]["memory"])
        self.assertEqual(e["settings"]["history_duration"], 4.0)

    def test_values_at_krr_minimum_are_flagged(self):
        e = entry("steady-overprovisioned")
        self.assertTrue(e["floor_applied"]["cpu"])       # 0.01 cores = --cpu-min 10
        self.assertTrue(e["floor_applied"]["memory"])    # 104857600 B = --mem-min 100
        self.assertFalse(entry("bursty-rare")["floor_applied"]["cpu"])   # 0.409

    def test_question_mark_means_no_value(self):
        e = entry("batch-periodic", "job")
        self.assertIsNone(e["recommended"]["cpu_request_cores"])
        self.assertIsNone(e["recommended"]["memory_request_bytes"])
        self.assertEqual(e["info"]["memory"], "Not enough data")
        self.assertFalse(e["floor_applied"]["memory"])


class InPack(unittest.TestCase):
    POD = "bursty-rare-7f7c547d44-68qr5"

    def build(self, krr_data):
        srv, url = serve(rules_for(self.POD))
        try:
            data = collect.fetch(Prom(url), NS, "1h")
        finally:
            srv.shutdown()
            srv.server_close()
        return collect.build_packs(NS, "1h", data, url, krr=krr_data)

    def test_krr_block_attached_when_matching(self):
        (p,) = self.build({(NS, "bursty-rare", "app"): entry("bursty-rare")})
        self.assertEqual(p["krr"]["recommended"]["cpu_request_cores"], 0.409)

    def test_no_krr_file_means_null_and_no_complaint(self):
        (p,) = self.build(None)
        self.assertIsNone(p["krr"])
        self.assertFalse(any("KRR" in x for x in p["limitations"]))

    def test_missing_entry_is_a_limitation(self):
        (p,) = self.build({})
        self.assertIsNone(p["krr"])
        self.assertTrue(any("no entry" in x for x in p["limitations"]))


class Verdicts(unittest.TestCase):
    def test_bursty_uses_krr_and_flags_disagreement(self):
        v = evaluate(with_krr(BURSTY, "bursty-rare"), LAB)
        self.assertEqual(v["cpu"]["candidate_source"], "krr")
        self.assertEqual(v["cpu"]["candidate_raw"], 0.409)
        self.assertEqual(v["cpu"]["recommendation"], "increase_supported")
        self.assertIn("K_DISAGREES", codes(v["cpu"]))

    def test_krr_at_its_floor_falls_back_to_the_formula(self):
        v = evaluate(with_krr(STEADY, "steady-overprovisioned"), LAB)
        for res in ("cpu", "memory"):
            self.assertEqual(v[res]["candidate_source"], "formula")
            self.assertIn("I_KRR_AT_FLOOR", codes(v[res]))
            self.assertEqual(v[res]["recommendation"], "decrease_ok")
        self.assertEqual(v["memory"]["candidate_adjusted"], 32 * MI)   # our floor, not KRR's 100Mi

    def test_krr_without_data_falls_back(self):
        p = with_krr(BATCH, "batch-periodic", "job")
        p["observed"]["coverage_seconds"] = 4 * 3600
        v = evaluate(p, LAB)
        self.assertEqual(v["memory"]["candidate_source"], "formula")
        self.assertIn("I_KRR_NO_DATA", codes(v["memory"]))
        self.assertEqual(v["memory"]["recommendation"], "decrease_ok")

    def test_leaky_memory_krr_agrees_with_formula(self):
        v = evaluate(with_krr(LEAKY, "leaky-memory"), LAB)
        self.assertEqual(v["memory"]["candidate_source"], "krr")
        self.assertNotIn("K_DISAGREES", codes(v["memory"]))
        self.assertEqual(v["memory"]["recommendation"], "increase_supported")

    def test_throttling_still_blocks_with_krr(self):
        v = evaluate(with_krr(THROTTLED, "throttled-cpu"), LAB)
        self.assertEqual(v["cpu"]["recommendation"], "decrease_blocked")

    def test_every_cited_field_still_exists(self):
        for p in (with_krr(BURSTY, "bursty-rare"), with_krr(STEADY, "steady-overprovisioned"),
                  with_krr(BATCH, "batch-periodic", "job"), with_krr(LEAKY, "leaky-memory")):
            v = evaluate(p, LAB)
            for res in ("cpu", "memory"):
                for r in v[res]["reasons"]:
                    for path, val in r["evidence"].items():
                        self.assertEqual(get(p, path), val, path)


if __name__ == "__main__":
    unittest.main()
