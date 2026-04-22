"""Evaluation harness for BMW agents.

Two kinds of evals:

1. **LLM-agent evals** (Agent 1): runs the real agent end-to-end per case and grades the
   tool-call trace. Needs GEMINI_API_KEY. Slow (~1 min per case). Skipped if no key.

2. **Deterministic-pipeline evals** (Agent 3): runs the hunter's deterministic path and
   checks the filter/ranking logic. Fast (<1 s total). Always runs.

Output: a pass/fail table + JSON summary at evals/results/<timestamp>.json.

Usage:
    python evals/run_evals.py              # run all
    python evals/run_evals.py --agent 1    # only Agent 1
    python evals/run_evals.py --agent 3    # only Agent 3
    python evals/run_evals.py --skip-llm   # skip LLM-dependent evals
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from hunter import scorer
from hunter.sources import Criteria, MockSource
from utils import cli
from utils.trace import load_trace

CASES_DIR = Path(__file__).parent / "cases"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Agent 1 (Deal Checker) — LLM-agent evals
# ---------------------------------------------------------------------------


def eval_deal_checker() -> list[dict]:
    import agent as agent_deal

    cases = json.loads((CASES_DIR / "deal_checker.json").read_text())["cases"]
    results: list[dict] = []

    for case in cases:
        t0 = time.time()
        print(cli.c(f"\n▸ {case['id']}", "teal", bold=True))
        print(cli.dim(f"  input: {case['input']}"))

        try:
            _, trace_path = agent_deal.run(case["input"], verbose=False)
            trace = load_trace(os.path.basename(trace_path))
        except Exception as e:
            results.append({"case": case["id"], "pass": False, "error": f"runtime: {e}", "duration_s": time.time() - t0})
            continue

        tool_events = trace.get("tool_events", []) if trace else []
        verdict_calls = [e for e in tool_events if e.get("name") == "compute_verdict"]
        lookup_calls = [e for e in tool_events if e.get("name") == "lookup_msrp"]

        observed_verdict = None
        if verdict_calls:
            observed_verdict = (verdict_calls[-1].get("result") or {}).get("verdict")

        observed_model = None
        if lookup_calls:
            res = lookup_calls[-1].get("result") or {}
            observed_model = res.get("matched_model")

        expected = case["expected_verdict_in"]
        passed = observed_verdict in expected
        model_ok = (observed_model is not None and case["expected_model"].lower() in (observed_model or "").lower())

        overall = passed and model_ok

        result = {
            "case": case["id"],
            "pass": overall,
            "observed_verdict": observed_verdict,
            "expected_verdict_in": expected,
            "observed_model": observed_model,
            "expected_model": case["expected_model"],
            "turns": trace.get("turns") if trace else None,
            "tool_count": len(tool_events),
            "duration_s": round(time.time() - t0, 1),
            "trace_path": os.path.basename(trace_path) if trace_path else None,
        }
        results.append(result)

        mark = cli.c("✓ PASS", "green", bold=True) if overall else cli.c("✗ FAIL", "red", bold=True)
        print(f"  {mark}  verdict={observed_verdict!r} model={observed_model!r} ({result['duration_s']}s, {result['turns']} turns)")

    return results


# ---------------------------------------------------------------------------
# Agent 3 (Hunter) — deterministic evals
# ---------------------------------------------------------------------------


def eval_hunter() -> list[dict]:
    cases = json.loads((CASES_DIR / "hunter.json").read_text())["cases"]
    results: list[dict] = []
    source = MockSource()

    for case in cases:
        print(cli.c(f"\n▸ {case['id']}", "teal", bold=True))
        c_input = case["criteria_input"]
        print(cli.dim(f"  criteria: {c_input}"))

        c = Criteria(**c_input)
        listings = source.search(c)
        scored = scorer.score_all(listings)
        ranked = scorer.rank(scored, top_n=10)

        failures: list[str] = []

        if "expect_min_results" in case and len(listings) < case["expect_min_results"]:
            failures.append(f"expected ≥{case['expect_min_results']} results, got {len(listings)}")

        if "expect_all_match_model" in case:
            needle = case["expect_all_match_model"].lower()
            bad = [l for l in listings if needle not in (l.get("model", "") + l.get("trim", "")).lower()]
            if bad:
                failures.append(f"{len(bad)} listings did not match model '{needle}'")

        if "expect_all_under_price" in case:
            cap = case["expect_all_under_price"]
            bad = [l for l in listings if l["asking_price_eur"] > cap]
            if bad:
                failures.append(f"{len(bad)} listings over price cap {cap}")

        if "expect_all_min_year" in case:
            year = case["expect_all_min_year"]
            bad = [l for l in listings if l["model_year"] < year]
            if bad:
                failures.append(f"{len(bad)} listings below min year {year}")

        if "expect_all_under_mileage" in case:
            cap = case["expect_all_under_mileage"]
            bad = [l for l in listings if l["mileage_km"] > cap]
            if bad:
                failures.append(f"{len(bad)} listings over mileage cap {cap}")

        if "top_deal_verdict_in" in case and ranked:
            top_verdict = ranked[0].get("verdict")
            if top_verdict not in case["top_deal_verdict_in"]:
                failures.append(f"top deal verdict {top_verdict!r} not in {case['top_deal_verdict_in']}")

        if case.get("check_ranking_descending_by_savings") and ranked:
            savings = [-l.get("delta_eur", 0) for l in ranked]
            if savings != sorted(savings, reverse=True):
                failures.append(f"ranking not descending by savings: {savings}")

        passed = not failures
        result = {
            "case": case["id"],
            "pass": passed,
            "results_count": len(listings),
            "ranked_count": len(ranked),
            "failures": failures,
        }
        results.append(result)

        mark = cli.c("✓ PASS", "green", bold=True) if passed else cli.c("✗ FAIL", "red", bold=True)
        print(f"  {mark}  {len(listings)} listings searched, {len(ranked)} ranked")
        for f in failures:
            print(cli.c(f"    - {f}", "red"))

    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def print_summary(all_results: dict) -> None:
    print()
    print(cli.banner("EVAL SUMMARY"))
    total_pass = total = 0
    for agent, results in all_results.items():
        if not results:
            continue
        passed = sum(1 for r in results if r["pass"])
        total_pass += passed
        total += len(results)
        pct = (passed / len(results) * 100) if results else 0
        color = "green" if pct == 100 else ("yellow" if pct >= 60 else "red")
        print(f"  {agent:20s}  {cli.c(f'{passed}/{len(results)} passed ({pct:.0f}%)', color, bold=True)}")
    print()
    overall_color = "green" if total_pass == total else "yellow" if total > 0 else "red"
    print(cli.c(f"  TOTAL: {total_pass}/{total}", overall_color, bold=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BMW agent evals")
    parser.add_argument("--agent", type=int, choices=[1, 3], help="Only run evals for this agent")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM-dependent evals (Agent 1)")
    args = parser.parse_args()

    has_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    results: dict = {}

    if args.agent in (None, 3):
        print(cli.banner("Agent 3 — Hunter (deterministic)"))
        results["hunter"] = eval_hunter()

    if args.agent in (None, 1) and not args.skip_llm:
        if not has_key:
            print(cli.c("\n[skipping Agent 1 evals — no GEMINI_API_KEY]", "yellow"))
        else:
            print(cli.banner("Agent 1 — Deal Checker (LLM agent)"))
            results["deal_checker"] = eval_deal_checker()

    print_summary(results)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{ts}_evals.json"
    path.write_text(json.dumps({"timestamp": ts, "results": results}, indent=2, default=str))
    print(cli.dim(f"\nResults saved: {path}"))

    all_passed = all(r["pass"] for group in results.values() for r in group)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
