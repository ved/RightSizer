import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from explainer import explain
from explainer.verdict import PROFILES, evaluate
from test_krr import with_krr
from test_verdict import BURSTY, THROTTLED

LAB = PROFILES["lab"]


def fake_ollama(reply, models=("qwen2.5-coder:14b",)):
    """Returns (server, url, seen) where seen collects the chat payloads."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"models": [{"name": m} for m in models]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(payload)
            content = reply(payload) if callable(reply) else reply
            self._send({"message": {"role": "assistant", "content": content}})

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", seen


EVERYTHING = ("no data, history, throttling, peak, above the request, OOM, near the limit, "
              "restarts, KRR")


def good_reply(v):
    return json.dumps({
        "headline": "test",
        "cpu": {"decision": v["cpu"]["recommendation"], "explanation": "cpu text " + EVERYTHING},
        "memory": {"decision": v["memory"]["recommendation"], "explanation": "mem text " + EVERYTHING}})


class Facts(unittest.TestCase):
    def setUp(self):
        self.pack = with_krr(BURSTY, "bursty-rare")
        self.v = evaluate(self.pack, LAB)
        self.facts = explain.facts_for(self.pack, self.v)
        self.text = "\n".join(self.facts)

    def test_verdict_and_candidate_are_stated(self):
        self.assertIn("[cpu] verdict: increase_supported", self.text)
        self.assertIn("[cpu] candidate request: 0.409 cores (source: krr)", self.text)
        self.assertIn("[cpu] current request: 0.050 cores", self.text)

    def test_reasons_are_included(self):
        self.assertIn("K_DISAGREES", self.text)

    def test_no_unformatted_floats(self):
        # figures are pre-rounded so the model copies them; long raw decimals invite arithmetic
        self.assertIsNone(re.search(r"\d\.\d{4,}", self.text))

    def test_throttled_pack_mentions_throttling(self):
        t = "\n".join(explain.facts_for(THROTTLED, evaluate(THROTTLED, LAB)))
        self.assertIn("cpu throttled periods: 60%", t)

    def test_prompt_forbids_new_numbers(self):
        msgs = explain.messages_for(self.facts)
        self.assertIn("Do not calculate", msgs[0]["content"])
        self.assertIn("FACTS:", msgs[1]["content"])


class Ask(unittest.TestCase):
    def setUp(self):
        self.pack = BURSTY
        self.v = evaluate(self.pack, LAB)

    def run_explain(self, reply):
        srv, url, seen = fake_ollama(reply)
        try:
            return explain.explain(self.pack, self.v, url, "qwen2.5-coder:14b", timeout=10, retries=0), seen
        finally:
            srv.shutdown()
            srv.server_close()

    def test_request_is_deterministic_and_schema_constrained(self):
        _, seen = self.run_explain(good_reply(self.v))
        p = seen[0]
        self.assertEqual(p["model"], "qwen2.5-coder:14b")
        self.assertEqual(p["options"]["temperature"], 0)
        self.assertFalse(p["stream"])
        self.assertEqual(p["format"], explain.RESPONSE_FORMAT)

    def test_matching_decisions_pass(self):
        r, _ = self.run_explain(good_reply(self.v))
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["parsed"]["cpu"]["explanation"].startswith("cpu text"))

    def test_wrong_decision_is_flagged(self):
        bad = json.dumps({"headline": "x",
                          "cpu": {"decision": "decrease_ok", "explanation": "x"},
                          "memory": {"decision": self.v["memory"]["recommendation"],
                                     "explanation": "x"}})
        r, _ = self.run_explain(bad)
        self.assertTrue(any("cpu: decision was 'decrease_ok'" in e for e in r["errors"]))

    def test_invalid_json_is_recorded_not_raised(self):
        r, _ = self.run_explain("not json")
        self.assertIsNone(r["parsed"])
        self.assertTrue(r["errors"])

    def test_schema_enum_matches_verdict_codes(self):
        from explainer import verdict
        for line in verdict.__doc__.splitlines():
            m = re.match(r"  (\w+)\s{2,}", line)
            if m and "_" in m.group(1):
                self.assertIn(m.group(1), explain.RECOMMENDATIONS)


class Cli(unittest.TestCase):
    def test_unreachable_ollama(self):
        with tempfile.TemporaryDirectory() as d:
            ev = __import__("pathlib").Path(d) / "ns"
            ev.mkdir()
            (ev / "w__app.json").write_text(json.dumps(BURSTY))
            self.assertEqual(explain.main(["--ollama", "http://127.0.0.1:1", "--evidence", d]), 2)

    def test_missing_model(self):
        srv, url, _ = fake_ollama("{}", models=("other:1b",))
        try:
            with tempfile.TemporaryDirectory() as d:
                ev = __import__("pathlib").Path(d) / "ns"
                ev.mkdir()
                (ev / "w__app.json").write_text(json.dumps(BURSTY))
                self.assertEqual(explain.main(["--ollama", url, "--evidence", d]), 2)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
