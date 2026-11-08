"""Deterministic verdict rules for rightsizing candidates.

Input: an evidence pack (see collect.py). Output: for CPU and memory, a
recommendation plus a list of reasons, each citing the evidence fields it used.
No LLM is involved here; the explainer later turns these reasons into prose and
must not add numbers that are not in the pack or the reasons.

Recommendations
  no_change              candidate is too close to the current request to matter
  no_request_set         nothing is configured, so there is no baseline to compare
  decrease_ok            lowering the request looks supported by the evidence
  decrease_blocked       lowering looks unsafe; see the blocking reasons
  increase_supported     the evidence shows pressure that a higher request would relieve
  increase_optional      candidate is higher, but nothing shows the workload is suffering
  insufficient_evidence  not enough history to decide

Thresholds are policy, not facts about Kubernetes. The profiles below are
illustrative starting points (the rightsizing book gives 7-day and 28-day
history windows and a 20% / 100m materiality filter as examples), not standards.
"""
import argparse
import json
import pathlib
import sys
from dataclasses import dataclass

MI = 2 ** 20
SCHEMA = "verdict/v1"


@dataclass(frozen=True)
class Policy:
    name: str
    min_history_s: float      # history needed before trusting a decrease
    rel_change: float         # minimum relative change to be worth acting on
    cpu_abs: float            # minimum absolute CPU change (cores)
    mem_abs: float            # minimum absolute memory change (bytes)
    cpu_floor: float = 0.01   # never recommend a CPU request below this (cores)
    mem_floor: float = 32 * MI
    throttle_block: float = 0.05   # throttled share of CFS periods that blocks a CPU decrease
    near_limit: float = 0.90       # working set / memory limit that blocks a memory decrease
    peak_factor: float = 2.0       # 1-minute CPU peak above this multiple of the candidate is risky


PROFILES = {
    "lab": Policy("lab", min_history_s=20 * 60, rel_change=0.20, cpu_abs=0.005, mem_abs=8 * MI),
    "dev": Policy("dev", min_history_s=7 * 86400, rel_change=0.20, cpu_abs=0.1, mem_abs=64 * MI),
    "production": Policy("production", min_history_s=28 * 86400, rel_change=0.20,
                         cpu_abs=0.1, mem_abs=64 * MI),
}


def get(pack, path):
    cur = pack
    for part in path.split("."):
        cur = cur[part]
    return cur


def _cores(v):
    return f"{v:.3f} cores"


def _bytes(v):
    return f"{v / MI:.0f}Mi"


def _reason(pack, code, severity, message, *paths):
    return {"code": code, "severity": severity, "message": message,
            "evidence": {p: get(pack, p) for p in paths}}


CPU = {
    "name": "cpu",
    "krr_key": "cpu_request_cores",
    "krr_floor": "cpu",
    "current": "configured.cpu_request_cores",
    "candidate": "candidate_krr_simple_formula.cpu_request_cores",
    "floor": "cpu_floor",
    "abs_min": "cpu_abs",
    "fmt": _cores,
}
MEM = {
    "name": "memory",
    "krr_key": "memory_request_bytes",
    "krr_floor": "memory",
    "current": "configured.memory_request_bytes",
    "candidate": "candidate_krr_simple_formula.memory_request_bytes",
    "floor": "mem_floor",
    "abs_min": "mem_abs",
    "fmt": _bytes,
}


KRR_DISAGREE = 0.25   # KRR and the formula differing by more than this is worth flagging


def _pick_candidate(pack, spec, notes):
    """Prefer KRR's real recommendation; fall back to the formula and say why.

    Returns (value, evidence path, source). `notes` collects info/warn reasons.
    """
    name, formula_path = spec["name"], spec["candidate"]
    k = pack.get("krr")
    if not k:
        return get(pack, formula_path), formula_path, "formula"
    key = spec["krr_key"]
    rec_path = f"krr.recommended.{key}"
    v = k["recommended"][key]
    if v is None:
        notes.append(_reason(
            pack, "I_KRR_NO_DATA", "info",
            f"KRR gave no {name} recommendation ({k['info'][name] or 'unknown reason'}); "
            f"using this tool's own figure instead.", f"krr.info.{name}"))
        return get(pack, formula_path), formula_path, "formula"
    if k["floor_applied"][spec["krr_floor"]]:
        notes.append(_reason(
            pack, "I_KRR_AT_FLOOR", "info",
            f"KRR's {name} figure equals KRR's own minimum, so it is not based on usage; "
            f"using this tool's own figure instead.",
            rec_path, f"krr.floor_applied.{name}"))
        return get(pack, formula_path), formula_path, "formula"
    return v, rec_path, "krr"


def _evaluate(pack, policy, spec):
    name, fmt = spec["name"], spec["fmt"]
    cur = get(pack, spec["current"])
    notes = []
    raw, cand_path, source = _pick_candidate(pack, spec, notes)
    cov = get(pack, "observed.coverage_seconds")
    out = {"current": cur, "candidate_raw": raw, "candidate_source": source,
           "candidate_adjusted": None, "change_ratio": None, "direction": None,
           "reasons": list(notes)}
    reasons = out["reasons"]

    def done(rec):
        out["recommendation"] = rec
        return out

    if raw is None or cov is None:
        reasons.append(_reason(
            pack, "E_NO_DATA", "block",
            f"No usable {name} usage data in the window, so no candidate can be judged.",
            "observed.coverage_seconds", cand_path))
        return done("insufficient_evidence")

    short = cov < policy.min_history_s
    short_reason = _reason(
        pack, "E_SHORT_HISTORY", "block",
        f"Only {cov / 3600:.1f} h of history; the {policy.name} profile wants at least "
        f"{policy.min_history_s / 3600:.1f} h before trusting a decrease.",
        "observed.coverage_seconds")

    floor = getattr(policy, spec["floor"])
    adj = raw
    if raw < floor:
        adj = floor
        reasons.append(_reason(
            pack, "I_FLOOR", "info",
            f"Candidate {fmt(raw)} is below the minimum {fmt(floor)}; using the minimum.",
            cand_path))
    out["candidate_adjusted"] = adj

    if cur is None or cur == 0:
        reasons.append(_reason(
            pack, "I_NO_BASELINE", "info",
            f"No {name} request is set, so the change cannot be compared with a current value.",
            spec["current"]))
        if short:
            reasons.append(short_reason)
            return done("insufficient_evidence")
        return done("no_request_set")

    delta = adj - cur
    rel = delta / cur
    out["change_ratio"] = rel
    if abs(rel) < policy.rel_change or abs(delta) < getattr(policy, spec["abs_min"]):
        reasons.append(_reason(
            pack, "I_IMMATERIAL", "info",
            f"Change from {fmt(cur)} to {fmt(adj)} is below the materiality threshold.",
            spec["current"], cand_path))
        return done("no_change")

    out["direction"] = "decrease" if delta < 0 else "increase"
    blocks, support, warns = [], [], []

    if source == "krr":
        mine = get(pack, spec["candidate"])
        if mine is not None and max(raw, mine) > 0 and abs(raw - mine) / max(raw, mine) > KRR_DISAGREE:
            warns.append(_reason(
                pack, "K_DISAGREES", "warn",
                f"KRR recommends {fmt(raw)} but this tool's own p95/peak-based figure is "
                f"{fmt(mine)}; they sample usage at different resolutions, so check the peak "
                f"before acting.", cand_path, spec["candidate"]))

    if name == "cpu":
        ratio = get(pack, "observed.cpu_throttle_ratio")
        p95 = get(pack, "observed.cpu_cores.p95")
        peak = get(pack, "observed.cpu_cores.peak_1m")
        if ratio is not None and ratio >= policy.throttle_block:
            msg = (f"CPU is throttled in {ratio:.0%} of periods: the container is hitting its "
                   f"CPU limit, so observed usage understates demand.")
            blocks.append(_reason(pack, "C_THROTTLED", "block", msg, "observed.cpu_throttle_ratio"))
            support.append(_reason(pack, "S_THROTTLED", "support", msg, "observed.cpu_throttle_ratio"))
        elif ratio is None and get(pack, "configured.cpu_limit_cores") is not None:
            warns.append(_reason(
                pack, "C_THROTTLE_UNKNOWN", "warn",
                "A CPU limit is set but throttling data is missing, so throttling cannot be ruled out.",
                "configured.cpu_limit_cores", "observed.cpu_throttle_ratio"))
        if peak is not None and peak > policy.peak_factor * adj:
            r = _reason(
                pack, "C_PEAK_ABOVE_CANDIDATE", "block" if delta < 0 else "warn",
                f"Short peak of {_cores(peak)} (1-minute rate) is more than "
                f"{policy.peak_factor:g}x the candidate {_cores(adj)}; a percentile can hide bursts.",
                "observed.cpu_cores.peak_1m", cand_path)
            (blocks if delta < 0 else warns).append(r)
        if p95 is not None and p95 > cur:
            support.append(_reason(
                pack, "S_USAGE_ABOVE_REQUEST", "support",
                f"p95 CPU {_cores(p95)} is above the request {_cores(cur)}.",
                "observed.cpu_cores.p95", spec["current"]))
    else:
        ws = get(pack, "observed.memory_bytes.max_working_set")
        lim = get(pack, "configured.memory_limit_bytes")
        restarts = get(pack, "observed.restarts") or 0
        if get(pack, "observed.oom_killed_seen"):
            msg = "An OOM kill was seen in the window; real demand exceeded what was measured."
            blocks.append(_reason(pack, "M_OOM_SEEN", "block", msg, "observed.oom_killed_seen"))
            support.append(_reason(pack, "S_OOM", "support", msg, "observed.oom_killed_seen"))
        if lim and ws is not None and ws >= policy.near_limit * lim:
            msg = (f"Peak working set {_bytes(ws)} is {ws / lim:.0%} of the memory limit "
                   f"{_bytes(lim)}.")
            blocks.append(_reason(pack, "M_NEAR_LIMIT", "block", msg,
                                  "observed.memory_bytes.max_working_set",
                                  "configured.memory_limit_bytes"))
            support.append(_reason(pack, "S_NEAR_LIMIT", "support", msg,
                                   "observed.memory_bytes.max_working_set",
                                   "configured.memory_limit_bytes"))
        if restarts > 0:
            blocks.append(_reason(
                pack, "M_RESTARTS", "block",
                f"{restarts} restart(s) in the window; samples before and after a restart are "
                f"not comparable.", "observed.restarts"))
        if ws is not None and ws > cur:
            support.append(_reason(
                pack, "S_USAGE_ABOVE_REQUEST", "support",
                f"Peak working set {_bytes(ws)} is above the request {_bytes(cur)}.",
                "observed.memory_bytes.max_working_set", spec["current"]))

    if delta < 0:
        reasons.extend(blocks + warns)
        if blocks:
            return done("decrease_blocked")
        if short:
            reasons.append(short_reason)
            return done("insufficient_evidence")
        return done("decrease_ok")

    reasons.extend(support + warns)
    if short:
        short_reason = dict(short_reason, severity="warn")
        reasons.append(short_reason)
    if support:
        return done("increase_supported")
    if short:
        return done("insufficient_evidence")
    return done("increase_optional")


def evaluate(pack, policy):
    return {
        "schema": SCHEMA,
        "policy": policy.name,
        "workload": pack["workload"],
        "cpu": _evaluate(pack, policy, CPU),
        "memory": _evaluate(pack, policy, MEM),
    }


def load_packs(evidence_dir):
    return [json.loads(p.read_text()) for p in sorted(pathlib.Path(evidence_dir).glob("*/*.json"))]


def write_verdicts(verdicts, out_dir):
    for v in verdicts:
        w = v["workload"]
        d = pathlib.Path(out_dir) / w["namespace"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{w['name']}__{w['container']}.json").write_text(json.dumps(v, indent=2) + "\n")


def summary(verdicts):
    head = f"{'workload':<28}{'cpu':<24}{'memory':<24}reasons"
    lines = [head, "-" * 100]
    for v in verdicts:
        codes = sorted({r["code"] for k in ("cpu", "memory") for r in v[k]["reasons"]
                        if r["severity"] in ("block", "warn", "support")})
        w = v["workload"]
        lines.append(f"{w['name'] + '/' + w['container']:<28}{v['cpu']['recommendation']:<24}"
                     f"{v['memory']['recommendation']:<24}{' '.join(codes)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply verdict rules to evidence packs.")
    ap.add_argument("--evidence", default="evidence")
    ap.add_argument("--out", default="verdicts")
    ap.add_argument("--profile", default="lab", choices=sorted(PROFILES))
    args = ap.parse_args(argv)
    packs = load_packs(args.evidence)
    if not packs:
        print(f"error: no evidence packs under {args.evidence}/ (run `make collect` first)",
              file=sys.stderr)
        return 2
    verdicts = [evaluate(p, PROFILES[args.profile]) for p in packs]
    write_verdicts(verdicts, args.out)
    print(summary(verdicts))
    print(f"\nProfile: {args.profile}. Wrote {len(verdicts)} verdict(s) to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
