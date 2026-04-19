"""
Citation post-processor.

The model cites evidence with inline markers, but local models (Gemma)
emit a few stylistic variants:

  [abc123def]                           short prefix
  [abc123def456...]                     full 64-hex
  [<id1>, <id2>]                        comma-joined inside one bracket
  [abc123] [def456]                     one pair of brackets per id
  abc123def456...                       no brackets at all

We handle all of them by scanning the answer for any hex run 6-70 chars
(bracketed or not), resolving each against the set of full 64-hex IDs
that appeared in tool outputs this turn, then rewriting the answer so
every resolved citation is in its own `[full-64-hex]` bracket. The UI
then only needs to understand ONE shape.

Resolution rules:
  - 64-hex verbatim match in the trace → verified.
  - 6-63 hex prefix that uniquely prefixes exactly one known ID → verified.
  - Everything else (ambiguous prefix, no match) → unverified; marker
    left as-is so the UI's warning state fires.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# Any hex run 6-70 chars, case-insensitive.
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{6,70}")

# For trace indexing — we only harvest full 64-hex strings as "known IDs".
_FULL_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")

_MIN_PREFIX_LEN = 6


# ---------------------------------------------------------------------------
# Collect the known-ID set from tool outputs
# ---------------------------------------------------------------------------


def _collect_known_ids_from_trace(trace: dict) -> set[str]:
    known: set[str] = set()
    for call in trace.get("tool_calls") or []:
        raw = call.get("output_json")
        if raw is None:
            continue
        _walk_and_collect(raw, known)
    return known


def _walk_and_collect(node: Any, known: set[str]) -> None:
    if isinstance(node, str):
        if _FULL_HEX_RE.fullmatch(node):
            known.add(node.lower())
        else:
            for match in _FULL_HEX_RE.finditer(node):
                known.add(match.group(0).lower())
    elif isinstance(node, dict):
        for v in node.values():
            _walk_and_collect(v, known)
    elif isinstance(node, list):
        for v in node:
            _walk_and_collect(v, known)


def _resolve(marker: str, known: set[str]) -> str | None:
    """Return the full 64-hex ID this marker maps to, or None."""
    m = marker.lower()
    if not re.fullmatch(r"[0-9a-f]{6,70}", m):
        return None
    if len(m) == 64 and m in known:
        return m
    if len(m) < _MIN_PREFIX_LEN:
        return None
    if len(m) > 64:
        candidate = m[:64]
        return candidate if candidate in known else None
    matches = [k for k in known if k.startswith(m)]
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------------------
# Rewrite: replace every hex run in the text with `[<resolved-full-id>]`
# ---------------------------------------------------------------------------


def _rewrite_text(text: str, resolver: dict[str, str | None]) -> str:
    """Replace every hex run (bracketed or not) with `[<full-id>]` when
    resolved; leave unresolved markers as-is (preserving any
    surrounding brackets so the UI can flag them)."""
    if not text:
        return text

    # Tokenize: walk the text, emit resolved hex-in-brackets for each
    # hex run we find, carry non-hex text through verbatim.
    out = []
    last = 0
    for m in _HEX_RUN_RE.finditer(text):
        # Text before this hex run.
        out.append(text[last : m.start()])
        hex_run = m.group(0)
        resolved = resolver.get(hex_run.lower())
        if resolved:
            # If we're already inside brackets in the surrounding text,
            # don't re-bracket — strip the surrounding bracket punctuation
            # from the output so we don't produce `[[id]]`. Simplest: always
            # emit `[resolved]` and strip any adjacent stray `[ ] ,` chars
            # from the carried context.
            # We'll post-process in _strip_stray_punctuation below.
            out.append(f"[{resolved}]")
        else:
            # Unresolved: keep the raw marker so the UI can flag.
            out.append(hex_run)
        last = m.end()
    out.append(text[last:])
    joined = "".join(out)
    return _collapse_citation_punctuation(joined)


_STRAY_COMMAS_BETWEEN_IDS_RE = re.compile(r"\]\s*,\s*\[")
_STRAY_EMPTY_BRACKETS_RE = re.compile(r"\[\s*\]")
_STRAY_DOUBLE_BRACKETS_RE = re.compile(r"\[\s*\[")
_STRAY_DOUBLE_CLOSE_BRACKETS_RE = re.compile(r"\]\s*\]")
_STRAY_LEADING_BRACKET_RE = re.compile(r"\[\s+(?=\[)")
_STRAY_WHITESPACE_INSIDE_RE = re.compile(r"\[\s+|\s+\]")


def _collapse_citation_punctuation(text: str) -> str:
    """Tidy up citation blocks after rewriting. Collapses patterns like
    `[<id1>, <id2>]` (which, after rewrite, looks like `[<id1>] , [<id2>]`
    because each hex run was independently bracketed) into clean
    `[<id1>] [<id2>]`."""
    # `], [` → `] [`
    text = _STRAY_COMMAS_BETWEEN_IDS_RE.sub("] [", text)
    # `[[` → `[`
    text = _STRAY_DOUBLE_BRACKETS_RE.sub("[", text)
    # `]]` → `]`
    text = _STRAY_DOUBLE_CLOSE_BRACKETS_RE.sub("]", text)
    # Empty brackets from stripped content: `[ ]` → ``
    text = _STRAY_EMPTY_BRACKETS_RE.sub("", text)
    # Collapse repeated whitespace around brackets.
    text = _STRAY_WHITESPACE_INSIDE_RE.sub(lambda m: "[" if "[" in m.group(0) else "]", text)
    # Finally, squeeze multi-spaces that may have appeared.
    text = re.sub(r" {2,}", " ", text)
    return text


def _rewrite_answer(answer: dict, resolver: dict[str, str | None]) -> dict:
    out = dict(answer)
    out["narrative"] = _rewrite_text(answer.get("narrative", ""), resolver)
    bullets = answer.get("evidence_bullets") or []
    if isinstance(bullets, list):
        out["evidence_bullets"] = [
            _rewrite_text(str(b), resolver) for b in bullets
        ]
    # next_artifact.id is a structured field — we validate it separately
    # but don't text-rewrite.
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_citations(answer: dict, trace: dict) -> dict:
    """
    Scan the answer for hex-run markers; resolve them against the
    trace; rewrite the answer with full IDs; return the structured
    citation outcome.

    Returns:
      rewritten_answer:     answer dict with resolved markers substituted
      cited:                list[str] of full IDs referenced (dedup, first-appearance)
      unverified_citations: list[str] of raw markers that could NOT be resolved
    """
    if not isinstance(answer, dict):
        return {
            "rewritten_answer": answer,
            "cited": [],
            "unverified_citations": [],
        }

    known = _collect_known_ids_from_trace(trace)

    # First pass: find every hex run in the answer text, build a
    # resolver map keyed by the raw (lowercased) hex run.
    resolver: dict[str, str | None] = {}
    raw_markers_in_order: list[str] = []

    texts_to_scan: list[str] = []
    texts_to_scan.append(str(answer.get("narrative") or ""))
    for b in answer.get("evidence_bullets") or []:
        if isinstance(b, str):
            texts_to_scan.append(b)

    seen_raw: set[str] = set()
    for text in texts_to_scan:
        for m in _HEX_RUN_RE.finditer(text):
            raw = m.group(0).lower()
            if raw in seen_raw:
                continue
            seen_raw.add(raw)
            raw_markers_in_order.append(raw)
            resolver[raw] = _resolve(raw, known)

    # next_artifact.id is a separate, structured citation — resolve
    # it and count it toward `cited`.
    artifact = answer.get("next_artifact")
    artifact_full: str | None = None
    if isinstance(artifact, dict):
        aid = artifact.get("id")
        if isinstance(aid, str):
            aid_lower = aid.lower()
            artifact_full = _resolve(aid_lower, known)
            # Also write the full ID back into the artifact so the UI
            # link works.
            if artifact_full:
                artifact = dict(artifact)
                artifact["id"] = artifact_full
                # If url is present and referenced a shortened id, fix.
                url = artifact.get("url")
                if isinstance(url, str) and aid in url:
                    artifact["url"] = url.replace(aid, artifact_full)
                answer = dict(answer)
                answer["next_artifact"] = artifact

    # Rewrite the answer text with resolved IDs.
    rewritten = _rewrite_answer(answer, resolver)

    # Build outputs: cited full IDs (first-appearance order) + unverified raws.
    cited: list[str] = []
    seen_full: set[str] = set()
    unverified: list[str] = []
    seen_un: set[str] = set()

    for raw in raw_markers_in_order:
        resolved = resolver.get(raw)
        if resolved:
            if resolved not in seen_full:
                seen_full.add(resolved)
                cited.append(resolved)
        else:
            if raw not in seen_un:
                seen_un.add(raw)
                unverified.append(raw)

    if artifact_full and artifact_full not in seen_full:
        seen_full.add(artifact_full)
        cited.append(artifact_full)

    if unverified:
        log.warning(
            "citation verifier: %d unverified marker(s): %s",
            len(unverified), ", ".join(unverified[:5]),
        )

    return {
        "rewritten_answer": rewritten,
        "cited": cited,
        "unverified_citations": unverified,
    }
