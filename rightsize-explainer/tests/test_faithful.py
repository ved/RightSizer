import json
import tempfile
import unittest
from pathlib import Path

from explainer import benchmark, explain, faithful, synthetic
from explainer.verdict import PROFILES, evaluate
from test_explain import EVERYTHING, fake_ollama
from test_verdict import BURSTY

FACTS = [
    "cpu p95 usage: 0.109 cores",
    "cpu 1-minute peak: 0.663 cores",
    "history observed: 4.7 h",
    "memory peak working set: 124Mi",
    "[cpu] current request: 0.050 cores",
    "[cpu] candidate request: 0.409 cores (source: krr)",
    "[cpu] change from current: +718%",
    "restarts in window: 17",
]


class Check(unittest.TestCase):
    def bad(self, text):
        return faithful.check(text, FACTS)

    def test_exact_copies_pass(self):
        self.assertEqual(self.bad("Raise CPU from 0.050 cores to 0.409 cores; peak 124Mi."), [])

    def test_trailing_zeros_do_not_matter(self):
        self.assertEqual(self.bad("The request is 0.05 cores."), [])

    def test_wrong_value_fails(self):
        self.assertEqual(self.bad("The request is 0.5 cores."), ["0.5 cores"])

    def test_unit_conversion_fails(self):
        self.assertEqual(self.bad("Set it to 409m."), ["409m"])

    def test_right_number_wrong_unit_fails(self):
        self.assertEqual(self.bad("Memory peaked at 0.409Mi."), ["0.409Mi"])

    def test_plain_counts_and_durations_checked(self):
        self.assertEqual(self.bad("It restarted 17 times over 4.7 h."), [])
        self.assertEqual(self.bad("It restarted 18 times."), ["18"])

    def test_identifiers_and_words_are_not_figures(self):
        self.assertEqual(self.bad("The p95 and p50 figures use a 1-minute peak."), [])
        self.assertEqual(self.bad("Memory is three times larger."), [])

    def test_percentages(self):
        self.assertEqual(self.bad("That is a 718% rise."), [])
        self.assertEqual(self.bad("That is a 71% rise."), ["71%"])

    def test_sentence_final_numbers(self):
        self.assertEqual(self.bad("Peak working set was 124Mi."), [])

    def test_each_bad_figure_listed_once(self):
        self.assertEqual(self.bad("9 and 9 and 8"), ["9", "8"])


class Retry(unittest.TestCase):
    def setUp(self):
        self.v = evaluate(BURSTY, PROFILES["lab"])

    def reply(self, cpu_text):
        return json.dumps({"headline": "h",
                           "cpu": {"decision": self.v["cpu"]["recommendation"], "explanation": cpu_text},
                           "memory": {"decision": self.v["memory"]["recommendation"],
                                      "explanation": "ok " + EVERYTHING}})

    def run_with(self, replies, retries=1):
        seq = iter(replies)
        srv, url, seen = fake_ollama(lambda payload: next(seq))
        try:
            return explain.explain(BURSTY, self.v, url, "m", 10, retries), seen
        finally:
            srv.shutdown()
            srv.server_close()

    def test_bad_figure_is_caught_then_repaired(self):
        r, seen = self.run_with([self.reply("Raise to 77.7 cores."), self.reply("Raise it. " + EVERYTHING)])
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["attempts"], 2)
        self.assertFalse(r["first_try_ok"])
        # the second request shows the model what it got wrong
        last_user = seen[1]["messages"][-1]["content"]
        self.assertIn("77.7 cores", last_user)

    def test_gives_up_after_retries(self):
        r, _ = self.run_with([self.reply("77.7 cores"), self.reply("88.8 cores")])
        self.assertEqual(r["attempts"], 2)
        self.assertTrue(any("88.8 cores" in e for e in r["errors"]))

    def test_no_retry_when_clean(self):
        r, seen = self.run_with([self.reply("Raise it. " + EVERYTHING)])
        self.assertEqual((r["attempts"], len(seen), r["first_try_ok"]), (1, 1, True))

    def test_retries_zero_means_one_call(self):
        r, seen = self.run_with([self.reply("77.7 cores")], retries=0)
        self.assertEqual(len(seen), 1)
        self.assertTrue(r["errors"])


class Coverage(unittest.TestCase):
    def test_leaving_out_the_blocker_is_caught(self):
        from test_verdict import THROTTLED
        v = evaluate(THROTTLED, PROFILES["lab"])
        self.assertEqual(explain.missing_topics("Lower it, it is fine.", v["cpu"]), ["throttling"])
        self.assertEqual(explain.missing_topics("Blocked: heavy CPU throttling.", v["cpu"]), [])

    def test_quoting_the_figures_covers_a_reason(self):
        from test_verdict import BURSTY as B
        v = evaluate(B, PROFILES["lab"])
        gaps = explain.missing_topics(
            "The request of 0.050 cores is below the p95 usage of 0.107 cores.", v["cpu"])
        self.assertNotIn("usage above request", gaps)
        # one of the two figures alone is not enough
        gaps = explain.missing_topics("The p95 usage is 0.107 cores.", v["cpu"])
        self.assertIn("usage above request", gaps)

    def test_feedback_includes_the_fact_to_restate(self):
        from test_verdict import THROTTLED
        v = evaluate(THROTTLED, PROFILES["lab"])
        parsed, problems = explain.check_output(json.dumps({
            "headline": "h",
            "cpu": {"decision": v["cpu"]["recommendation"], "explanation": "Keep it."},
            "memory": {"decision": v["memory"]["recommendation"], "explanation": "ok " + EVERYTHING}}),
            v, explain.facts_for(THROTTLED, v))
        self.assertTrue(any("throttled in 60% of periods" in p for p in problems))

    def test_info_reasons_are_not_required(self):
        v = evaluate(synthetic.CASES["idle-overprovisioned"][0], PROFILES["lab"])
        self.assertEqual(explain.missing_topics("Lower it.", v["cpu"]), [])

    def test_topics_deduplicated(self):
        from test_verdict import LEAKY
        v = evaluate(LEAKY, PROFILES["lab"])
        gaps = explain.missing_topics("Raise it.", v["memory"])
        self.assertEqual(len(gaps), len(set(gaps)))
        self.assertIn("OOM kill", gaps)

    def test_omission_triggers_a_retry(self):
        from test_verdict import THROTTLED
        v = evaluate(THROTTLED, PROFILES["lab"])
        def reply(text):
            return json.dumps({"headline": "h",
                               "cpu": {"decision": v["cpu"]["recommendation"], "explanation": text},
                               "memory": {"decision": v["memory"]["recommendation"],
                                          "explanation": "ok"}})
        seq = iter([reply("Do not lower it."), reply("Do not lower it: CPU throttling.")])
        srv, url, seen = fake_ollama(lambda p: next(seq))
        try:
            r = explain.explain(THROTTLED, v, url, "m", 10, 1)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual((r["attempts"], r["errors"]), (2, []))
        self.assertIn("leaves out: throttling", seen[1]["messages"][-1]["content"])


class Synthetic(unittest.TestCase):
    EXPECT = {
        "idle-overprovisioned": ("decrease_ok", "decrease_ok"),
        "cpu-throttled": ("decrease_blocked", "decrease_ok"),
        "memory-leak-oom": ("decrease_ok", "increase_supported"),
        "bursty-cpu": ("increase_supported", "decrease_ok"),
        "short-history": ("increase_supported", "insufficient_evidence"),
        "no-usage-data": ("insufficient_evidence", "insufficient_evidence"),
        "no-request-set": ("no_request_set", "no_request_set"),
        "near-memory-limit": ("no_change", "decrease_blocked"),
        "restarts-only": ("decrease_ok", "decrease_blocked"),
        "peak-blocks-cut": ("decrease_blocked", "decrease_ok"),
        "tiny-difference": ("no_change", "no_change"),
        "production-3-days": ("insufficient_evidence", "insufficient_evidence"),
    }

    def test_every_case_produces_its_intended_verdict(self):
        self.assertEqual(set(self.EXPECT), set(synthetic.CASES))
        for name, (pack, profile) in synthetic.CASES.items():
            v = evaluate(pack, PROFILES[profile])
            self.assertEqual((v["cpu"]["recommendation"], v["memory"]["recommendation"]),
                             self.EXPECT[name], name)

    def test_facts_build_for_every_case(self):
        for name, (pack, profile) in synthetic.CASES.items():
            facts = explain.facts_for(pack, evaluate(pack, PROFILES[profile]))
            self.assertTrue(facts, name)


class Bench(unittest.TestCase):
    def test_table_and_files(self):
        def reply(payload):
            facts = payload["messages"][1]["content"]
            v = {}
            for res in ("cpu", "memory"):
                line = [x for x in facts.splitlines() if x.startswith(f"- [{res}] verdict: ")][0]
                v[res] = line.split("verdict: ")[1]
            return json.dumps({"headline": "h",
                               "cpu": {"decision": v["cpu"], "explanation": "fine " + EVERYTHING},
                               "memory": {"decision": v["memory"], "explanation": "fine " + EVERYTHING}})
        srv, url, _ = fake_ollama(reply, models=("a:1", "b:2"))
        try:
            with tempfile.TemporaryDirectory() as d:
                rc = benchmark.main(["--models", "a:1,b:2", "--ollama", url,
                                     "--evidence", str(Path(d) / "none"), "--out", d])
                md = (Path(d) / "results.md").read_text()
                rows = json.loads((Path(d) / "results.json").read_text())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rc, 0)
        self.assertEqual(len(rows), 2 * len(synthetic.CASES))
        self.assertIn("| a:1 | 12 | 12/12 (100%)", md)
        self.assertTrue(all(r["final_ok"] for r in rows))

    def test_uninstalled_model_is_refused(self):
        srv, url, _ = fake_ollama("{}", models=("a:1",))
        try:
            self.assertEqual(benchmark.main(["--models", "nope:1", "--ollama", url]), 2)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
