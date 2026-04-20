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
    client: httpx.Client,
    agent_url: str,
    spec: QuestionSpec,
    *,
    backend: str | None = None,
) -> QuestionResult:
    body: dict[str, Any] = {"question": spec.question}
    if spec.scope:
        body["scope"] = spec.scope
    if backend:
        body["backend"] = backend

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


# ---------------------------------------------------------------------------
# Per-backend aggregates for the compare table
# ---------------------------------------------------------------------------


_EMPTY_AGG: dict[str, Any] = {
    "count": 0,
    "pass": 0, "warn": 0, "fail": 0,
    "avg_latency_ms": 0, "avg_iterations": 0, "avg_tool_calls": 0,
    "avg_citations": 0, "avg_unverified": 0,
    "avg_tokens_in": 0, "avg_tokens_out": 0,
    "total_cost_usd": 0.0,
}


def _aggregate(results: list[QuestionResult]) -> dict[str, Any]:
    if not results:
        return dict(_EMPTY_AGG)
    pass_ = sum(1 for r in results if r.verdict == "PASS")
    warn = sum(1 for r in results if r.verdict == "WARN")
    fail = sum(1 for r in results if r.verdict == "FAIL")
    lats = [r.latency_ms for r in results]
    iters = [r.iterations for r in results]
    tcs = [r.tool_call_count for r in results]
    cites = [r.citations for r in results]
    unvs = [r.unverified for r in results]
    tokens_in = []
    tokens_out = []
    cost_total = 0.0
    for r in results:
        t = (r.response.get("trace") or {})
        tokens_in.append(int(t.get("total_prompt_tokens") or 0))
        tokens_out.append(int(t.get("total_completion_tokens") or 0))
        cost_total += float(t.get("estimated_cost_usd") or 0.0)
    return {
        "count":         len(results),
        "pass":          pass_,
        "warn":          warn,
        "fail":          fail,
        "avg_latency_ms": int(sum(lats) / len(lats)) if lats else 0,
        "avg_iterations": round(sum(iters) / len(iters), 2) if iters else 0,
        "avg_tool_calls": round(sum(tcs) / len(tcs), 2) if tcs else 0,
        "avg_citations":  round(sum(cites) / len(cites), 2) if cites else 0,
        "avg_unverified": round(sum(unvs) / len(unvs), 2) if unvs else 0,
        "avg_tokens_in":  int(sum(tokens_in) / len(tokens_in)) if tokens_in else 0,
        "avg_tokens_out": int(sum(tokens_out) / len(tokens_out)) if tokens_out else 0,
        "total_cost_usd": round(cost_total, 4),
    }


def _run_seed_set(
    client: httpx.Client,
    agent_url: str,
    specs: list[QuestionSpec],
    *,
    backend: str | None,
) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    label = backend or "default"
    print(f"  [backend={label}]")
    for spec in specs:
        print(f"    → {spec.name:36s}", end=" ", flush=True)
        r = _run_one(client, agent_url, spec, backend=backend)
        results.append(r)
        print(
            f"{r.verdict:4s} {r.latency_ms:6d}ms "
            f"iter={r.iterations} tc={r.tool_call_count} "
            f"cite={r.citations}/unv={r.unverified}"
        )
        for reason in r.reasons:
            print(f"          · {reason}")
    return results


def _print_compare_table(
    specs: list[QuestionSpec],
    results_by_backend: dict[str, list[QuestionResult]],
) -> None:
    backends = list(results_by_backend.keys())
    print()
    print("=" * 100)
    print("COMPARISON (prompt-driven parity — same prompt + tools across backends)")
    print("=" * 100)

    # Header
    col_w = 14
    name_w = 36
    header = f"{'question':<{name_w}}"
    for b in backends:
        header += f"{b:>{col_w}}"
    print(header)
    print("-" * name_w, *[("-" * col_w).rjust(col_w) for _ in backends])

    # Rows
    idx: dict[str, dict[str, QuestionResult]] = {
        b: {r.name: r for r in rs} for b, rs in results_by_backend.items()
    }
    for spec in specs:
        row = f"{spec.name:<{name_w}}"
        for b in backends:
            r = idx[b].get(spec.name)
            if r is None:
                cell = "skipped"
            else:
                cell = f"{r.verdict} {r.latency_ms / 1000:5.1f}s"
            row += f"{cell:>{col_w}}"
        print(row)

    # Aggregates
    print()
    print(f"{'-' * name_w}", *[("-" * col_w).rjust(col_w) for _ in backends])
    metrics = [
        ("scoreboard P/W/F",    lambda a: f"{a['pass']}/{a['warn']}/{a['fail']}"),
        ("avg latency",          lambda a: f"{a['avg_latency_ms']/1000:.1f}s"),
        ("avg iterations",       lambda a: f"{a['avg_iterations']}"),
        ("avg tool_calls",       lambda a: f"{a['avg_tool_calls']}"),
        ("avg citations",        lambda a: f"{a['avg_citations']}"),
        ("avg unverified",       lambda a: f"{a['avg_unverified']}"),
        ("avg tokens in",        lambda a: f"{a['avg_tokens_in']}"),
        ("avg tokens out",       lambda a: f"{a['avg_tokens_out']}"),
        ("total est cost (USD)", lambda a: f"${a['total_cost_usd']:.4f}"),
    ]
    aggs = {b: _aggregate(rs) for b, rs in results_by_backend.items()}
    for label, fn in metrics:
        row = f"{label:<{name_w}}"
        for b in backends:
            row += f"{fn(aggs[b]):>{col_w}}"
        print(row)

    # Fairness disclaimer.
    print()
    print(
        "NB: comparison is across ONE protocol (prompt-driven). "
        "Native tool_use / function-calling (Claude / OpenAI) would likely "
        "improve those two backends further — deliberately unused for\n"
        "apples-to-apples benchmarking. See docs/AGENT.md."
    )


def _write_csv(
    path: str,
    specs: list[QuestionSpec],
    results_by_backend: dict[str, list[QuestionResult]],
) -> None:
    import csv
    rows = []
    fields = [
        "backend", "question", "verdict", "latency_ms", "iterations",
        "tool_call_count", "citations", "unverified",
        "tokens_in", "tokens_out", "cost_usd",
    ]
    for backend, results in results_by_backend.items():
        by_name = {r.name: r for r in results}
        for spec in specs:
            r = by_name.get(spec.name)
            trace = (r.response.get("trace") if r else None) or {}
            rows.append({
                "backend":         backend,
                "question":        spec.name,
                "verdict":         r.verdict if r else "SKIPPED",
                "latency_ms":      r.latency_ms if r else 0,
                "iterations":      r.iterations if r else 0,
                "tool_call_count": r.tool_call_count if r else 0,
                "citations":       r.citations if r else 0,
                "unverified":      r.unverified if r else 0,
                "tokens_in":       int(trace.get("total_prompt_tokens") or 0),
                "tokens_out":      int(trace.get("total_completion_tokens") or 0),
                "cost_usd":        float(trace.get("estimated_cost_usd") or 0.0),
            })
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("AGENT_URL", "http://localhost:8500"))
    ap.add_argument("--json-out", help="optional path to write full results JSON")
    ap.add_argument("--csv-out", help="optional path to write flat CSV (one row per backend × question)")
    ap.add_argument(
        "--only", default=None,
        help="comma-separated seed names to run (skip the rest)",
    )
    ap.add_argument(
        "--backend", default=None,
        help="force every /ask in this run to use this backend key "
             "(e.g. gemma / claude / gpt5mini). Default: agent's default.",
    )
    ap.add_argument(
        "--compare", default=None,
        help="comma-separated backends to compare (e.g. gemma,claude,gpt5mini). "
             "Runs the seed set once per backend and prints a comparison table.",
    )
    args = ap.parse_args()

    specs = _load_specs()
    if args.only:
        allowed = {x.strip() for x in args.only.split(",")}
        specs = [s for s in specs if s.name in allowed]
    if not specs:
        print("no seed questions loaded")
        return 1

    if args.compare and args.backend:
        print("--compare and --backend are mutually exclusive")
        return 2

    t_total = time.perf_counter()
    results_by_backend: dict[str, list[QuestionResult]] = {}

    with httpx.Client(timeout=240.0) as client:
        if args.compare:
            backends = [b.strip() for b in args.compare.split(",") if b.strip()]
            # Skip unavailable backends cleanly rather than failing the run.
            try:
                live = client.get(f"{args.agent}/v1/agent/backends").json()
                available = {b["name"] for b in live.get("backends", []) if b.get("is_available")}
            except Exception:
                available = set(backends)
            print(f"comparing {backends} against {args.agent} "
                  f"({len(specs)} questions × {len(backends)} backends)")
            print()
            for b in backends:
                if available and b not in available:
                    print(f"  [backend={b}] skipping — not available in registry")
                    results_by_backend[b] = []
                    continue
                results_by_backend[b] = _run_seed_set(client, args.agent, specs, backend=b)
        else:
            backend_label = args.backend or "default"
            print(f"running {len(specs)} seed question(s) against {args.agent} "
                  f"[backend={backend_label}]")
            print()
            results_by_backend[backend_label] = _run_seed_set(
                client, args.agent, specs, backend=args.backend,
            )

    elapsed = time.perf_counter() - t_total

    # Summary.
    if args.compare:
        _print_compare_table(specs, results_by_backend)
        # Overall exit code: 0 only if every available backend has 0 FAILs.
        any_fail = any(
            any(r.verdict == "FAIL" for r in rs)
            for rs in results_by_backend.values()
            if rs
        )
        rc = 1 if any_fail else 0
    else:
        (label, results), = results_by_backend.items()
        n_pass = sum(1 for r in results if r.verdict == "PASS")
        n_warn = sum(1 for r in results if r.verdict == "WARN")
        n_fail = sum(1 for r in results if r.verdict == "FAIL")
        print()
        print(f"scoreboard: {n_pass} PASS · {n_warn} WARN · {n_fail} FAIL "
              f"({len(results)} total, {elapsed:.1f}s)")
        rc = 1 if n_fail else 0

    # Optional dumps.
    if args.json_out:
        payload = {}
        for backend, results in results_by_backend.items():
            payload[backend] = [
                {
                    "name": r.name, "verdict": r.verdict, "reasons": r.reasons,
                    "latency_ms": r.latency_ms, "iterations": r.iterations,
                    "tool_call_count": r.tool_call_count,
                    "citations": r.citations, "unverified": r.unverified,
                    "response": r.response,
                }
                for r in results
            ]
        pathlib.Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"wrote full results to {args.json_out}")

    if args.csv_out:
        _write_csv(args.csv_out, specs, results_by_backend)
        print(f"wrote flat CSV to {args.csv_out}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
