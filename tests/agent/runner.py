"""
Seed-question harness for the M11 agent.

Fires each question in seed_questions.yaml at the live /v1/agent/ask
endpoint and scores the response. Exits 0 on all-pass, 1 on any fail.

    cd ~/panoptic
    .venv/bin/python tests/agent/runner.py
    .venv/bin/python tests/agent/runner.py --agent http://localhost:8500 --json-out results.json

Scoring per question:
  PASS — every assertion met
  WARN — latency overshot but content assertions passed
  FAIL — any content assertion failed (wrong tool, too few citations,
         too many unverified, missing hedge, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

HERE = pathlib.Path(__file__).resolve().parent
QUESTIONS_PATH = HERE / "seed_questions.yaml"


# Hedge phrases that satisfy `hedge_required: true`. Case-insensitive
# substring match anywhere in the narrative.
_HEDGE_PHRASES = [
    "no evidence",
    "no direct evidence",
    "no explicit evidence",
    "no indication",
    "strongest evidence suggests",
    "tool output indicates",
    "appears to",
    "appears that",
    "not find",
    "not been found",
    "did not find",
    "not reported",
    "no record",
    "no mention",
    "nothing in the tool output",
    "nothing was found",
    "no support",
    "no supporting evidence",
    "no data",
]


@dataclass
class QuestionSpec:
    name: str
    question: str
    scope: dict | None
    expected_tools: list[str]
    min_citations: int
    max_unverified: int
    max_iterations: int
    max_latency_ms: int
    hedge_required: bool
    disallow_next_artifact: bool


@dataclass
class QuestionResult:
    name: str
    verdict: str          # PASS / WARN / FAIL
    reasons: list[str]
    latency_ms: int
    iterations: int
    tool_call_count: int
    citations: int
    unverified: int
    response: dict


def _load_specs() -> list[QuestionSpec]:
    data = yaml.safe_load(QUESTIONS_PATH.read_text())
    out: list[QuestionSpec] = []
    for q in data.get("questions", []):
        out.append(
            QuestionSpec(
                name=q["name"],
                question=q["question"],
                scope=q.get("scope"),
                expected_tools=list(q.get("expected_tools") or []),
                min_citations=int(q.get("min_citations", 0)),
                max_unverified=int(q.get("max_unverified", 0)),
                max_iterations=int(q.get("max_iterations", 8)),
                max_latency_ms=int(q.get("max_latency_ms", 30000)),
                hedge_required=bool(q.get("hedge_required", False)),
                disallow_next_artifact=bool(q.get("disallow_next_artifact", False)),
            )
        )
    return out


def _contains_hedge(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in _HEDGE_PHRASES)


def _score(spec: QuestionSpec, resp: dict, latency_ms: int) -> QuestionResult:
    trace = resp.get("trace") or {}
    answer = resp.get("answer") or {}
    citations = resp.get("citations") or []
    unverified = trace.get("unverified_citations") or []
    tool_names = [tc.get("name") for tc in (trace.get("tool_calls") or [])]
    narrative = (answer.get("narrative") or "").strip()

    reasons: list[str] = []
    verdict = "PASS"

    # ---- content assertions ----
    if spec.expected_tools:
        if not any(t in tool_names for t in spec.expected_tools):
            reasons.append(
                f"no expected tool fired (wanted one of {spec.expected_tools}, "
                f"got {tool_names})"
            )

    if len(citations) < spec.min_citations:
        reasons.append(
            f"only {len(citations)} citation(s), wanted ≥{spec.min_citations}"
        )

    if len(unverified) > spec.max_unverified:
        reasons.append(
            f"{len(unverified)} unverified citation(s), allowed ≤{spec.max_unverified}"
        )

    iters = int(trace.get("iterations") or 0)
    if iters > spec.max_iterations:
        reasons.append(f"{iters} iterations, allowed ≤{spec.max_iterations}")

    if spec.hedge_required and not _contains_hedge(narrative):
        reasons.append(
            "hedge language required but narrative was not hedged (evidence-discipline fail)"
        )

    if spec.disallow_next_artifact and answer.get("next_artifact"):
        reasons.append(
            "next_artifact should be null for this question (no actionable follow-on)"
        )

    # ---- latency is a WARN, not a FAIL (content trumps) ----
    latency_warning = latency_ms > spec.max_latency_ms
    if latency_warning:
        reasons.append(
            f"latency {latency_ms}ms exceeded max {spec.max_latency_ms}ms"
        )

    if reasons and not (len(reasons) == 1 and latency_warning):
        verdict = "FAIL"
    elif latency_warning:
        verdict = "WARN"

    return QuestionResult(
        name=spec.name,
        verdict=verdict,
        reasons=reasons,
        latency_ms=latency_ms,
        iterations=iters,
        tool_call_count=int(trace.get("tool_call_count") or 0),
        citations=len(citations),
        unverified=len(unverified),
        response=resp,
    )


def _run_one(
    client: httpx.Client, agent_url: str, spec: QuestionSpec,
) -> QuestionResult:
    body: dict[str, Any] = {"question": spec.question}
    if spec.scope:
        body["scope"] = spec.scope

    t0 = time.perf_counter()
    try:
        r = client.post(f"{agent_url}/v1/agent/ask", json=body, timeout=240.0)
        r.raise_for_status()
        resp = r.json()
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return QuestionResult(
            name=spec.name, verdict="FAIL",
            reasons=[f"request failed: {type(exc).__name__}: {exc}"],
            latency_ms=latency_ms,
            iterations=0, tool_call_count=0,
            citations=0, unverified=0,
            response={},
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return _score(spec, resp, latency_ms)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("AGENT_URL", "http://localhost:8500"))
    ap.add_argument("--json-out", help="optional path to write full results JSON")
    ap.add_argument(
        "--only", default=None,
        help="comma-separated seed names to run (skip the rest)",
    )
    args = ap.parse_args()

    specs = _load_specs()
    if args.only:
        allowed = {x.strip() for x in args.only.split(",")}
        specs = [s for s in specs if s.name in allowed]
    if not specs:
        print("no seed questions loaded")
        return 1

    print(f"running {len(specs)} seed question(s) against {args.agent}")
    print()

    results: list[QuestionResult] = []
    t_total = time.perf_counter()
    with httpx.Client(timeout=240.0) as client:
        for spec in specs:
            print(f"  → {spec.name:36s}", end=" ", flush=True)
            r = _run_one(client, args.agent, spec)
            results.append(r)
            print(
                f"{r.verdict:4s} {r.latency_ms:6d}ms "
                f"iter={r.iterations} tc={r.tool_call_count} "
                f"cite={r.citations}/unv={r.unverified}"
            )
            for reason in r.reasons:
                print(f"        · {reason}")

    elapsed = time.perf_counter() - t_total
    n_pass = sum(1 for r in results if r.verdict == "PASS")
    n_warn = sum(1 for r in results if r.verdict == "WARN")
    n_fail = sum(1 for r in results if r.verdict == "FAIL")
    print()
    print(
        f"scoreboard: {n_pass} PASS · {n_warn} WARN · {n_fail} FAIL "
        f"({len(results)} total, {elapsed:.1f}s)"
    )

    if args.json_out:
        payload = []
        for r in results:
            payload.append(
                {
                    "name": r.name,
                    "verdict": r.verdict,
                    "reasons": r.reasons,
                    "latency_ms": r.latency_ms,
                    "iterations": r.iterations,
                    "tool_call_count": r.tool_call_count,
                    "citations": r.citations,
                    "unverified": r.unverified,
                    "response": r.response,
                }
            )
        pathlib.Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"wrote full results to {args.json_out}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
