"""Number-faithfulness check: every figure in the model's text must appear in the facts.

A figure is a number with an optional unit. "0.5 cores" fails against a fact of
"0.050 cores" (different value); "50m" fails because no fact is written in
millicores (different unit). Numbers inside identifiers such as p95 are ignored
on both sides. Trailing zeros do not matter: 0.05 equals 0.050.

This checks figures only. It does not check that a sentence is true, only that
it did not introduce a number the code never produced.
"""
import re

_TOKEN = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)"
    r"(?:\s?(cores?|Mi|Gi|%)|(m)(?![A-Za-z])|\s?(h|x)(?![A-Za-z]))?")


def _unit(m):
    u = m.group(2) or m.group(3) or m.group(4)
    return "cores" if u == "core" else u


def tokens(text):
    """List of (as written, value, unit) for every figure in text."""
    return [(m.group(0).strip(), round(float(m.group(1)), 6), _unit(m))
            for m in _TOKEN.finditer(text)]


def check(text, facts):
    """Return the figures in `text` that do not appear in `facts` (list of strings)."""
    fact_tokens = tokens("\n".join(facts))
    pairs = {(v, u) for _, v, u in fact_tokens}
    values = {v for _, v, _ in fact_tokens}
    bad = []
    for raw, v, u in tokens(text):
        ok = v in values if u is None else (v, u) in pairs
        if not ok and raw not in bad:
            bad.append(raw)
    return bad
