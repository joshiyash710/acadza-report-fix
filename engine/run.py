"""CLI: python -m engine.run [--provider replay|anthropic|openai|gemini] [--model NAME] [--lazy] [--estimate]"""
import argparse
from collections import Counter

from .pipeline import run, write_outputs

CHARS_PER_TOKEN = 3.5  # HTML/MathML-heavy prompts tokenize denser than prose (~4); estimate, +/-25%


def tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def report_tokens(meter: list, label: str) -> dict:
    by = {}
    for m in meter:
        b = by.setdefault(m["stage"], {"calls": 0, "in": 0, "out": 0})
        b["calls"] += 1
        b["in"] += tokens(m["system"] + m["user"])
        b["out"] += tokens(m["output"])
    tot = {k: sum(b[k] for b in by.values()) for k in ("calls", "in", "out")}
    print(f"\n{label}")
    print(f"  {'stage':12s} {'calls':>5s} {'input tok':>10s} {'output tok':>11s}")
    for st, b in by.items():
        print(f"  {st:12s} {b['calls']:5d} {b['in']:10,d} {b['out']:11,d}")
    print(f"  {'TOTAL':12s} {tot['calls']:5d} {tot['in']:10,d} {tot['out']:11,d}")
    return tot


def main():
    ap = argparse.ArgumentParser(description="Acadza report -> fix triage engine")
    ap.add_argument("--provider", default="replay", choices=["replay", "anthropic", "openai", "gemini"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--lazy", action="store_true", help="run the second blind solve only when it can change a decision")
    ap.add_argument("--estimate", action="store_true", help="print token estimates for a live run (always-2 vs lazy); writes nothing")
    a = ap.parse_args()
    if a.estimate:
        full = run(a.provider, a.model, lazy=False)
        lazy = run(a.provider, a.model, lazy=True)
        t1 = report_tokens(full["meter"], "Solver always runs twice")
        t2 = report_tokens(lazy["meter"], "Lazy solver (second solve only when needed)")
        same = [r for r in full["results"]] == [r for r in lazy["results"]]
        print(f"\nlazy saves {t1['calls'] - t2['calls']} calls and {(t1['in'] + t1['out']) - (t2['in'] + t2['out']):,} tokens "
              f"({100 * (1 - (t2['in'] + t2['out']) / (t1['in'] + t1['out'])):.0f}%); results.json identical: {same}")
        return
    out = run(a.provider, a.model, lazy=a.lazy)
    write_outputs(out)
    res = out["results"]
    m = out["meta"]
    print(f"{len(res)} reports -> {len(out['cases'])} cases | provider={m['provider']} solver={m['solver_mode']} live_calls={m['live_calls']} cache_hits={m['cache_hits']}")
    print("routes:  ", dict(Counter(r["route"] for r in res)))
    print("verdicts:", dict(Counter(r["verdict"] for r in res)))
    print("unreported defects found:", sum(len(c["unreported"]) for c in out["cases"]))
    print("wrote results.json, unreported_defects.json and console/data/cases.json")


if __name__ == "__main__":
    main()
