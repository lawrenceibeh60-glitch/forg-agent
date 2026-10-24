"""Opt-in model evaluation suite for basic reasoning, continuity, and honesty.

Run from the forge directory: python evals/run.py
This calls the configured provider and may consume API tokens. It never invokes tools.
"""
import asyncio
import json
from pathlib import Path

import llm
from prompts import SYSTEM

CASES = Path(__file__).with_name("cases.json")


async def run_case(case):
    messages = [
        {"role": "system", "content": SYSTEM + "\nFor this evaluation, answer directly. Do not call tools. Do not reveal hidden reasoning."},
        *case["messages"],
    ]
    output = ""
    async for kind, value in llm.stream(messages, []):
        if kind == "text":
            output += value
    lowered = output.casefold()
    checks = []
    for term in case.get("must_contain", []):
        checks.append((term, term.casefold() in lowered))
    if case.get("must_contain_any"):
        terms = case["must_contain_any"]
        checks.append(("one of: " + ", ".join(terms), any(t.casefold() in lowered for t in terms)))
    passed = all(ok for _, ok in checks) and bool(output.strip())
    return {"id": case["id"], "description": case["description"], "passed": passed,
            "checks": [{"criterion": term, "passed": ok} for term, ok in checks],
            "response": output[:1200]}


async def main():
    cases = json.loads(CASES.read_text())
    results = []
    for case in cases:
        try:
            result = await run_case(case)
        except Exception as exc:
            result = {"id": case["id"], "passed": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(("PASS" if result["passed"] else "FAIL") + " " + case["id"])
    report = {"model": llm.active().get("model", "unknown"), "passed": sum(r["passed"] for r in results),
              "total": len(results), "results": results}
    out = Path("data/eval-results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print("Detailed report:", out)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
