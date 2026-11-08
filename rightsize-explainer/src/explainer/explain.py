"""Turn a verdict into plain language with a local LLM (Ollama).

The model never sees raw evidence and never decides anything. It receives a short
list of FACTS, already formatted the way they should be quoted, and the verdict
the rules produced. It returns JSON that must repeat that verdict. Numbers in its
text are checked afterwards (step 5), so the prompt tells it to copy figures
exactly and never calculate.
"""
import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

from . import faithful
from . import verdict as V

SCHEMA = "explanation/v1"
RECOMMENDATIONS = ["no_change", "no_request_set", "decrease_ok", "decrease_blocked",
                   "increase_supported", "increase_optional", "insufficient_evidence"]

RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "cpu": {"type": "object", "properties": {
            "decision": {"type": "string", "enum": RECOMMENDATIONS},
            "explanation": {"type": "string"}}, "required": ["decision", "explanation"]},
        "memory": {"type": "object", "properties": {
            "decision": {"type": "string", "enum": RECOMMENDATIONS},
            "explanation": {"type": "string"}}, "required": ["decision", "explanation"]},
    },
    "required": ["headline", "cpu", "memory"],
}

SYSTEM = """You explain Kubernetes resource-request verdicts to an engineer.
A rules engine has already made every decision. You only put it into words.

Rules:
- Use ONLY the figures in FACTS. Copy each figure exactly as written, with its unit.
- Do not calculate, convert units, round, or estimate. Do not introduce any other number.
- "decision" must be exactly the verdict given in FACTS for that resource.
- Say what the verdict is, then mention EVERY reason in FACTS of severity block, warn or support
  for that resource (blocking reasons first). Skip reasons of severity info unless they explain
  a number you quote.
- If a fact says data is missing or limited, say so plainly. Never fill the gap.
- At most three sentences per resource. Plain language, no advice beyond the verdict.
- "headline" is ONE sentence naming the workload and what is decided for CPU and for memory,
  in plain words (for example: "web: lower CPU is safe, memory must not be cut")."""


def facts_for(pack, v):
    """Flat list of fact lines. Every number the model may quote appears here."""
    o, c = pack["observed"], pack["configured"]
    w = pack["workload"]
    lines = [f"workload: {w['name']} (container {w['container']}, namespace {w['namespace']})",
             f"history observed: {o['coverage_seconds'] / 3600:.1f} h"
             if o["coverage_seconds"] is not None else "history observed: none"]
    lines.append(f"policy profile: {v['policy']}")
    cpu = o["cpu_cores"]
    if cpu["p95"] is not None:
        lines.append(f"cpu p95 usage: {V._cores(cpu['p95'])}")
    if cpu["peak_1m"] is not None:
        lines.append(f"cpu 1-minute peak: {V._cores(cpu['peak_1m'])}")
    if o["cpu_throttle_ratio"] is not None:
        lines.append(f"cpu throttled periods: {o['cpu_throttle_ratio']:.0%}")
    if o["memory_bytes"]["max_working_set"] is not None:
        lines.append(f"memory peak working set: {V._bytes(o['memory_bytes']['max_working_set'])}")
    if c["memory_limit_bytes"]:
        lines.append(f"memory limit: {V._bytes(c['memory_limit_bytes'])}")
    lines.append(f"restarts in window: {o['restarts']}" if o["restarts"] is not None
                 else "restarts in window: unknown")
    lines.append(f"oom kill seen: {'yes' if o['oom_killed_seen'] else 'no'}")
    for name, fmt in (("cpu", V._cores), ("memory", V._bytes)):
        r = v[name]
        lines.append(f"[{name}] verdict: {r['recommendation']}")
        if r["current"] is not None:
            lines.append(f"[{name}] current request: {fmt(r['current'])}")
        else:
            lines.append(f"[{name}] current request: not set")
        if r["candidate_raw"] is not None:
            lines.append(f"[{name}] candidate request: {fmt(r['candidate_raw'])} "
                         f"(source: {r['candidate_source']})")
        if r["candidate_adjusted"] is not None and r["candidate_adjusted"] != r["candidate_raw"]:
            lines.append(f"[{name}] candidate after minimum floor: {fmt(r['candidate_adjusted'])}")
        if r["change_ratio"] is not None:
            lines.append(f"[{name}] change from current: {r['change_ratio']:+.0%}")
        for reason in r["reasons"]:
            lines.append(f"[{name}] reason {reason['code']} ({reason['severity']}): "
                         f"{reason['message']}")
    for lim in pack.get("limitations", []):
        lines.append(f"limitation: {lim}")
    return lines


# Words that show an explanation mentioned a reason. Crude on purpose: it is deterministic,
# so "did it leave out the blocker?" can be tested without another model.
TOPICS = {
    "no usable data": ("no data", "no usable", "missing", "not enough", "no usage", "insufficient"),
    "short history": ("history",),
    "throttling": ("throttl",),
    "short peak": ("peak", "burst", "spike"),
    "usage above request": ("above", "exceed", "higher than", "more than"),
    "OOM kill": ("oom", "out-of-memory", "out of memory", "killed"),
    "memory near limit": ("limit",),
    "restarts": ("restart",),
    "KRR disagreement": ("krr",),
}
CODE_TOPIC = {
    "E_NO_DATA": "no usable data", "E_SHORT_HISTORY": "short history",
    "C_THROTTLED": "throttling", "S_THROTTLED": "throttling", "C_THROTTLE_UNKNOWN": "throttling",
    "C_PEAK_ABOVE_CANDIDATE": "short peak", "S_USAGE_ABOVE_REQUEST": "usage above request",
    "M_OOM_SEEN": "OOM kill", "S_OOM": "OOM kill", "M_NEAR_LIMIT": "memory near limit",
    "S_NEAR_LIMIT": "memory near limit", "M_RESTARTS": "restarts", "K_DISAGREES": "KRR disagreement",
}


def missing_reasons(text, result):
    """(topic, reason message) for block/warn/support reasons the explanation never covers.

    A reason counts as covered if the text names its topic OR quotes every figure that
    reason's message contains (so "request 0.200 cores is below usage 0.613 cores" covers
    "usage above request" without needing the word "above").
    """
    low = text.lower()
    have = {(v, u) for _, v, u in faithful.tokens(text)}
    have_values = {v for _, v, _ in faithful.tokens(text)}
    out, seen = [], set()
    for r in result["reasons"]:
        topic = CODE_TOPIC.get(r["code"])
        if not topic or r["severity"] not in ("block", "warn", "support") or topic in seen:
            continue
        if any(w in low for w in TOPICS[topic]):
            continue
        figs = faithful.tokens(r["message"])
        if figs and all((v in have_values) if u is None else ((v, u) in have)
                        for _, v, u in figs):
            continue
        seen.add(topic)
        out.append((topic, r["message"]))
    return out


def missing_topics(text, result):
    return [t for t, _ in missing_reasons(text, result)]


def select_caveats(pack):
    """Choose the limitations worth showing. Chosen by code, not by the model."""
    keep = ("History covers", "Workload runs intermittently", "The KRR output has no entry")
    return [x for x in pack.get("limitations", []) if x.startswith(keep)]


def messages_for(facts):
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "FACTS:\n" + "\n".join(f"- {x}" for x in facts)
             + "\n\nReturn the explanation as JSON."}]


class OllamaError(RuntimeError):
    pass


def _call(url, payload=None, timeout=300):
    req = urllib.request.Request(
        url, data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", "")
        except (ValueError, OSError):
            detail = ""
        raise OllamaError(f"Ollama returned {e.code}: {detail}") from e
    except OSError as e:
        raise OllamaError(f"cannot reach Ollama at {url}: {e}") from e


def installed_models(host):
    return [m["name"] for m in _call(f"{host.rstrip('/')}/api/tags", timeout=10).get("models", [])]


def ask(host, model, messages, timeout=300):
    """One deterministic chat call with schema-constrained output. Returns the raw text."""
    body = _call(f"{host.rstrip('/')}/api/chat", {
        "model": model, "messages": messages, "stream": False,
        "format": RESPONSE_FORMAT,
        "options": {"temperature": 0, "seed": 7, "num_ctx": 8192}}, timeout)
    try:
        return body["message"]["content"]
    except (KeyError, TypeError) as e:
        raise OllamaError(f"unexpected Ollama response: {str(body)[:200]}") from e


def check_output(raw, v, facts):
    """Return (parsed or None, list of problems)."""
    try:
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
    except (ValueError, AssertionError):
        return None, ["output was not valid JSON"]
    problems = []
    texts = [str(parsed.get("headline", ""))]
    for res in ("cpu", "memory"):
        part = parsed.get(res) if isinstance(parsed.get(res), dict) else {}
        got = part.get("decision")
        if got != v[res]["recommendation"]:
            problems.append(
                f"{res}: decision was {got!r} but the verdict in FACTS is {v[res]['recommendation']!r}")
        texts.append(str(part.get("explanation", "")))
        gaps = missing_reasons(texts[-1], v[res])
        if gaps:
            problems.append(f"{res}: explanation leaves out: {', '.join(t for t, _ in gaps)}. "
                            f"Say it using these facts: " + " ".join(m for _, m in gaps))
    bad = faithful.check(" ".join(texts), facts)
    if bad:
        problems.append("figures not in FACTS: " + ", ".join(bad))
    return parsed, problems


def explain(pack, v, host, model, timeout=300, retries=1):
    """Ask, check, and on a failed check show the model its own problems and ask once more."""
    facts = facts_for(pack, v)
    msgs = messages_for(facts)
    attempts = []
    for _ in range(retries + 1):
        raw = ask(host, model, msgs, timeout)
        parsed, problems = check_output(raw, v, facts)
        attempts.append((raw, parsed, problems))
        if not problems:
            break
        msgs = msgs + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "That answer broke these rules:\n- " + "\n- ".join(problems)
             + "\nRewrite the JSON. Use only figures from FACTS, copied exactly, and repeat the "
               "verdicts given in FACTS."}]
    raw, parsed, problems = attempts[-1]
    return {"schema": SCHEMA, "model": model, "workload": pack["workload"], "facts": facts,
            "raw": raw, "parsed": parsed, "errors": problems, "attempts": len(attempts),
            "first_try_ok": not attempts[0][2], "caveats": select_caveats(pack)}


def write(results, out_dir):
    for r in results:
        w = r["workload"]
        d = pathlib.Path(out_dir) / w["namespace"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{w['name']}__{w['container']}.json").write_text(json.dumps(r, indent=2) + "\n")


def render(r):
    w = r["workload"]
    head = f"== {w['name']}/{w['container']}  [{r['model']}]"
    if r["parsed"] is None:
        return f"{head}\n   ERROR: {'; '.join(r['errors'])}"
    p = r["parsed"]
    lines = [head, f"   {p.get('headline', '')}",
             f"   CPU:    {p.get('cpu', {}).get('explanation', '')}",
             f"   Memory: {p.get('memory', {}).get('explanation', '')}"]
    for c in r["caveats"]:
        lines.append(f"   caveat: {c}")
    if r["attempts"] > 1 and not r["errors"]:
        lines.append("   (needed a second attempt after a failed check)")
    for e in r["errors"]:
        lines.append(f"   CHECK FAILED: {e}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Explain verdicts with a local LLM.")
    ap.add_argument("--evidence", default="evidence")
    ap.add_argument("--out", default="explanations")
    ap.add_argument("--profile", default="lab", choices=sorted(V.PROFILES))
    ap.add_argument("--model", default="qwen2.5-coder:14b")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--only", help="only workloads whose name contains this text")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--retries", type=int, default=1, help="extra attempts after a failed check")
    args = ap.parse_args(argv)
    packs = V.load_packs(args.evidence)
    if args.only:
        packs = [p for p in packs if args.only in p["workload"]["name"]]
    if not packs:
        print(f"error: no evidence packs under {args.evidence}/ (run `make collect` first)",
              file=sys.stderr)
        return 2
    try:
        have = installed_models(args.ollama)
        if args.model not in have:
            print(f"error: model {args.model!r} is not installed. Installed: {', '.join(have)}",
                  file=sys.stderr)
            return 2
        results = [explain(p, V.evaluate(p, V.PROFILES[args.profile]), args.ollama, args.model,
                           args.timeout, args.retries) for p in packs]
    except OllamaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    write(results, args.out)
    print("\n\n".join(render(r) for r in results))
    bad = sum(1 for r in results if r["errors"])
    print(f"\n{len(results)} explanation(s) written to {args.out}/; {bad} still failing checks.")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
