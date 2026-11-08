"""Score local models on the explainer task.

For every (model, case) the model explains a verdict. We record whether it repeated the
right decision, whether every figure it wrote came from the facts, whether that held on
the first try, whether a retry fixed it, and how long it took.
"""
import argparse
import json
import pathlib
import sys
import time

from . import explain as E
from . import synthetic
from . import verdict as V


def cases(evidence_dir=None):
    """Synthetic cases plus, if available, the real evidence packs."""
    out = {f"synthetic/{k}": v for k, v in synthetic.CASES.items()}
    if evidence_dir and pathlib.Path(evidence_dir).exists():
        for p in V.load_packs(evidence_dir):
            out[f"lab/{p['workload']['name']}"] = (p, "lab")
    return out


def run(host, models, case_map, retries=1, timeout=300):
    rows = []
    for model in models:
        for name, (pack, profile) in case_map.items():
            v = V.evaluate(pack, V.PROFILES[profile])
            t0 = time.time()
            try:
                r = E.explain(pack, v, host, model, timeout, retries)
                err = None
            except E.OllamaError as e:
                r, err = None, str(e)
            rows.append({
                "model": model, "case": name, "seconds": round(time.time() - t0, 1),
                "error": err,
                "first_try_ok": bool(r and r["first_try_ok"]),
                "final_ok": bool(r and not r["errors"]),
                "problems": [] if r is None else r["errors"],
            })
    return rows


def table(rows):
    models = list(dict.fromkeys(r["model"] for r in rows))
    lines = ["| Model | Cases | Passed first try | Passed after retry | Avg seconds |",
             "|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        n = len(rs)
        first = sum(r["first_try_ok"] for r in rs)
        final = sum(r["final_ok"] for r in rs)
        avg = sum(r["seconds"] for r in rs) / n
        lines.append(f"| {m} | {n} | {first}/{n} ({first / n:.0%}) | {final}/{n} ({final / n:.0%}) "
                     f"| {avg:.1f} |")
    fails = [r for r in rows if not r["final_ok"]]
    if fails:
        lines += ["", "Still failing after retry:"]
        lines += [f"- {r['model']} / {r['case']}: {r['error'] or '; '.join(r['problems'])}"
                  for r in fails]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark local models on verdict explanations.")
    ap.add_argument("--models", required=True, help="comma-separated Ollama model names")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--evidence", default="evidence", help="also use real packs from here")
    ap.add_argument("--no-synthetic", action="store_true")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default="benchmark")
    args = ap.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    try:
        have = E.installed_models(args.ollama)
    except E.OllamaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    missing = [m for m in models if m not in have]
    if missing:
        print(f"error: not installed: {', '.join(missing)}. Installed: {', '.join(have)}",
              file=sys.stderr)
        return 2
    case_map = cases(args.evidence)
    if args.no_synthetic:
        case_map = {k: v for k, v in case_map.items() if not k.startswith("synthetic/")}
    rows = run(args.ollama, models, case_map, args.retries, args.timeout)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    md = table(rows)
    (out / "results.md").write_text(md + "\n")
    print(md)
    print(f"\nWrote {out}/results.json and {out}/results.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
