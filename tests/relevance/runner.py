"""
Relevance harness runner for M1.

Executes the queries defined in queries.yaml against the live Search API
and prints a scoreboard comparing actual top-k results to the ground-truth
serial (and optionally camera) each query was designed to surface.

    .venv/bin/python tests/relevance/runner.py
    .venv/bin/python tests/relevance/runner.py --top-k 5 --api http://localhost:8600

Scoring:
    PASS  — expected serial is in results rank 1..3
    WARN  — expected serial is in results rank 4..top_k
    FAIL  — expected serial not in top-k results
    (Per-query scoring looks at images branch first, then summaries.)

Exit code: 0 if zero FAILs, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass

import httpx
import yaml

HERE = pathlib.Path(__file__).resolve().parent
QUERIES_PATH = HERE / "queries.yaml"


@dataclass
class QuerySpec:
    name: str
    query: str
    expected_serials: list[str]
    expected_cameras: list[str]
    notes: str


@dataclass
class QueryResult:
    name: str
    verdict: str            # PASS / WARN / FAIL
    branch: str             # images / summaries / (none)
    rank: int | None        # 1-based rank of the first expected serial match
    top_k_serials: list[str]
    timing_ms: int
    caption_preview: str    # short snippet from the top hit for eyeballing


def _load_queries() -> list[QuerySpec]:
    data = yaml.safe_load(QUERIES_PATH.read_text())
    out: list[QuerySpec] = []
    for q in data.get("queries", []):
        out.append(
            QuerySpec(
                name=q["name"],
                query=q["query"],
                expected_serials=list(q.get("expected_serials") or []),
                expected_cameras=list(q.get("expected_cameras") or []),
                notes=q.get("notes", ""),
            )
        )
    return out


def _run_one(spec: QuerySpec, api: str, top_k: int) -> QueryResult:
    body = {"query": spec.query, "top_k": top_k}
    resp = httpx.post(f"{api}/v1/search", json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    images = data["results"]["images"] or []
    summaries = data["results"]["summaries"] or []

    # Pick the branch that gives the best rank for any expected serial.
    def _rank(branch_rows: list[dict]) -> tuple[int | None, list[str]]:
        serials = [r.get("serial_number") for r in branch_rows]
        for i, sn in enumerate(serials):
            if sn in spec.expected_serials:
                return i + 1, serials
        return None, serials

    img_rank, img_serials = _rank(images)
    sum_rank, sum_serials = _rank(summaries)

    branch = "(none)"
    rank = None
    top_serials: list[str] = []
    preview = ""

    if img_rank is not None and (sum_rank is None or img_rank <= sum_rank):
        branch = "images"
        rank = img_rank
        top_serials = img_serials
        preview = (images[0].get("caption_text") or "")[:90]
    elif sum_rank is not None:
        branch = "summaries"
        rank = sum_rank
        top_serials = sum_serials
        preview = (summaries[0].get("summary") or "")[:90]
    else:
        # neither branch matched — surface whatever's on top for eyeballing
        if images:
            top_serials = img_serials
            preview = (images[0].get("caption_text") or "")[:90]
        elif summaries:
            top_serials = sum_serials
            preview = (summaries[0].get("summary") or "")[:90]

    if rank is None:
        verdict = "FAIL"
    elif rank <= 3:
        verdict = "PASS"
    else:
        verdict = "WARN"

    return QueryResult(
        name=spec.name,
        verdict=verdict,
        branch=branch,
        rank=rank,
        top_k_serials=top_serials,
        timing_ms=data.get("timing_ms", {}).get("total", 0),
        caption_preview=preview,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("SEARCH_API", "http://localhost:8600"))
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--json-out", help="optional path to write full JSON results")
    args = ap.parse_args()

    specs = _load_queries()
    if not specs:
        print("no queries found")
        return 1

    print(f"running {len(specs)} queries against {args.api} (top_k={args.top_k})")
    print()

    results: list[QueryResult] = []
    t0 = time.perf_counter()
    for spec in specs:
        try:
            r = _run_one(spec, args.api, args.top_k)
        except Exception as exc:
            print(f"  {spec.name:24s} ERROR {exc}")
            r = QueryResult(spec.name, "FAIL", "(err)", None, [], 0, f"exception: {exc}")
        results.append(r)
        rank_str = f"#{r.rank}" if r.rank else "—"
        print(
            f"  {r.verdict:4s} {spec.name:24s} "
            f"branch={r.branch:10s} rank={rank_str:3s} "
            f"t={r.timing_ms:4d}ms  "
            f"preview={r.caption_preview!r}"
        )
    elapsed = time.perf_counter() - t0

    n_pass = sum(1 for r in results if r.verdict == "PASS")
    n_warn = sum(1 for r in results if r.verdict == "WARN")
    n_fail = sum(1 for r in results if r.verdict == "FAIL")

    print()
    print(f"scoreboard: {n_pass} PASS  {n_warn} WARN  {n_fail} FAIL  ({len(results)} total, {elapsed:.1f}s)")

    if args.json_out:
        payload = [
            {
                "name": r.name,
                "verdict": r.verdict,
                "branch": r.branch,
                "rank": r.rank,
                "top_serials": r.top_k_serials,
                "timing_ms": r.timing_ms,
                "caption_preview": r.caption_preview,
            }
            for r in results
        ]
        pathlib.Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"full results written to {args.json_out}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
