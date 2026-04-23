"""
Tests for services.search_api.pull_frame — the M14 on-demand pull path.

Covers:
  - Deterministic image_id (same (serial, camera, bucket, ts) = same ID)
  - 15-min bucket-snap correctness
  - `already_exists` short-circuit on pre-existing row
  - Rate limit enforcement
  - 404 surfaces when trailer has no recording
  - Auth + network errors from Continuum propagate with correct status codes

DB interaction is isolated behind a small fake engine fixture so these
tests don't require postgres. The rate-limit deque is reset between
tests via the exported helper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.search_api.pull_frame import (
    PullFrameError,
    _bucket_bounds,
    _to_utc,
    reset_rate_limit_for_tests,
    run_pull_frame,
)
from services.search_api.schemas import PullFrameRequest
from shared.clients.continuum import (
    ContinuumAuthError,
    ContinuumFrameResponse,
    ContinuumNetworkError,
)
from shared.schemas.image import generate_image_id


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal context-manager-aware fake for SQLAlchemy Connection."""

    def __init__(self, fetchone_returns: list):
        self._fetchone_iter = iter(fetchone_returns)
        self.executed: list[tuple[str, dict]] = []
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        result = MagicMock()
        try:
            result.fetchone.return_value = next(self._fetchone_iter)
        except StopIteration:
            result.fetchone.return_value = None
        return result

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeEngine:
    """Engine stub that hands out connections from a queue."""

    def __init__(self, conns: list[_FakeConn]):
        self._conns = list(conns)

    def connect(self):
        return self._conns.pop(0)


def _frame(jpeg: bytes = b"\xff\xd8\xff\xd9") -> ContinuumFrameResponse:
    return ContinuumFrameResponse(
        jpeg_bytes=jpeg,
        data_uri="data:image/jpeg;base64,//0=",
        requested_ts=datetime(2026, 4, 23, 14, 35, tzinfo=UTC),
    )


def _client_with_frame(frame=None, *, fetch_exc=None):
    cl = MagicMock()
    if fetch_exc is not None:
        cl.fetch_frame.side_effect = fetch_exc
    else:
        cl.fetch_frame.return_value = frame
    return cl


def _req(**over) -> PullFrameRequest:
    base = {
        "serial_number": "TEST-SN",
        "camera_id":     "cam-A",
        "timestamp_utc": datetime(2026, 4, 23, 14, 35, 12, tzinfo=UTC),
    }
    base.update(over)
    return PullFrameRequest.model_validate(base)


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    reset_rate_limit_for_tests()
    yield
    reset_rate_limit_for_tests()


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------


class TestBucketBounds:
    def test_snaps_to_15_min_start(self):
        ts = datetime(2026, 4, 23, 14, 37, 12, tzinfo=UTC)
        start, end = _bucket_bounds(ts)
        assert start == datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        assert end   == datetime(2026, 4, 23, 14, 45, tzinfo=UTC)

    def test_exact_boundary_stays(self):
        ts = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        start, end = _bucket_bounds(ts)
        assert start == ts

    def test_naive_ts_treated_as_utc(self):
        utc_aware = _to_utc(datetime(2026, 4, 23, 14, 30))
        assert utc_aware.tzinfo == UTC


class TestImageIdDeterminism:
    def test_same_inputs_produce_same_image_id(self):
        a = generate_image_id(
            "SN", "cam", "2026-04-23T14:30:00+00:00", "2026-04-23T14:45:00+00:00",
            "pulled", 1776890112000,
        )
        b = generate_image_id(
            "SN", "cam", "2026-04-23T14:30:00+00:00", "2026-04-23T14:45:00+00:00",
            "pulled", 1776890112000,
        )
        assert a == b

    def test_different_timestamp_ms_differs(self):
        a = generate_image_id(
            "SN", "cam", "2026-04-23T14:30:00+00:00", "2026-04-23T14:45:00+00:00",
            "pulled", 1776890112000,
        )
        b = generate_image_id(
            "SN", "cam", "2026-04-23T14:30:00+00:00", "2026-04-23T14:45:00+00:00",
            "pulled", 1776890113000,
        )
        assert a != b


# ---------------------------------------------------------------------------
# Happy path + dedup
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_creates_row_and_returns_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.search_api.pull_frame.IMAGE_STORAGE_ROOT", str(tmp_path))

        existing_conn = _FakeConn([None])       # _load_existing → None
        insert_conn = _FakeConn([
            SimpleNamespace(image_id="ignored"),         # INSERT RETURNING
            SimpleNamespace(job_id="job-1"),             # caption job insert
        ])
        engine = _FakeEngine([existing_conn, insert_conn])

        redis = MagicMock()
        resp = run_pull_frame(
            _req(),
            engine,
            continuum_client=_client_with_frame(_frame()),
            redis_client=redis,
        )

        assert resp.status == "created"
        assert resp.caption_status == "pending"
        assert resp.bucket_start_utc == datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
        assert resp.bucket_end_utc   == datetime(2026, 4, 23, 14, 45, tzinfo=UTC)
        # Caption job enqueued to Redis once.
        assert redis.method_calls  # some call happened via enqueue_job()
        # File written.
        assert (tmp_path / "TEST-SN" / "cam-A" / "2026" / "04" / "23").exists()

    def test_already_exists_short_circuits(self):
        existing_conn = _FakeConn([
            SimpleNamespace(storage_path="/dev/null", caption_status="success"),
        ])
        engine = _FakeEngine([existing_conn])
        client = _client_with_frame(_frame())

        resp = run_pull_frame(_req(), engine, continuum_client=client, redis_client=MagicMock())

        assert resp.status == "already_exists"
        assert resp.caption_status == "success"
        # Trailer was NOT contacted — short-circuit saved the round-trip.
        assert client.fetch_frame.call_count == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_404_when_no_recording(self):
        existing_conn = _FakeConn([None])
        engine = _FakeEngine([existing_conn])
        client = _client_with_frame(None)   # Continuum returned None

        with pytest.raises(PullFrameError) as exc:
            run_pull_frame(_req(), engine, continuum_client=client, redis_client=MagicMock())
        assert exc.value.status_code == 404

    def test_auth_error_propagates_as_403(self):
        existing_conn = _FakeConn([None])
        engine = _FakeEngine([existing_conn])
        client = _client_with_frame(fetch_exc=ContinuumAuthError("nope"))

        with pytest.raises(PullFrameError) as exc:
            run_pull_frame(_req(), engine, continuum_client=client, redis_client=MagicMock())
        assert exc.value.status_code == 403

    def test_network_error_propagates_as_502(self):
        existing_conn = _FakeConn([None])
        engine = _FakeEngine([existing_conn])
        client = _client_with_frame(fetch_exc=ContinuumNetworkError("down"))

        with pytest.raises(PullFrameError) as exc:
            run_pull_frame(_req(), engine, continuum_client=client, redis_client=MagicMock())
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_within_limit_passes(self):
        # Synthesize 9 accepted pulls — tenth should still pass.
        for _ in range(10):
            engine = _FakeEngine([
                _FakeConn([SimpleNamespace(storage_path="/dev/null", caption_status="pending")]),
            ])
            run_pull_frame(
                _req(),
                engine,
                continuum_client=_client_with_frame(_frame()),
                redis_client=MagicMock(),
            )

    def test_exceeds_limit_raises_429(self):
        # 10 accepted, 11th should hit rate limit.
        for _ in range(10):
            engine = _FakeEngine([
                _FakeConn([SimpleNamespace(storage_path="/dev/null", caption_status="pending")]),
            ])
            run_pull_frame(
                _req(),
                engine,
                continuum_client=_client_with_frame(_frame()),
                redis_client=MagicMock(),
            )

        engine = _FakeEngine([
            _FakeConn([SimpleNamespace(storage_path="/dev/null", caption_status="pending")]),
        ])
        with pytest.raises(PullFrameError) as exc:
            run_pull_frame(
                _req(),
                engine,
                continuum_client=_client_with_frame(_frame()),
                redis_client=MagicMock(),
            )
        assert exc.value.status_code == 429

    def test_window_slides(self):
        """Advancing the clock past the window frees the budget."""
        fake_time = [1000.0]

        def now():
            return fake_time[0]

        # Burn the budget.
        for _ in range(10):
            engine = _FakeEngine([
                _FakeConn([SimpleNamespace(storage_path="/dev/null", caption_status="pending")]),
            ])
            run_pull_frame(
                _req(),
                engine,
                continuum_client=_client_with_frame(_frame()),
                redis_client=MagicMock(),
                now_fn=now,
            )

        # Advance well past the 60-second window.
        fake_time[0] += 120.0

        engine = _FakeEngine([
            _FakeConn([SimpleNamespace(storage_path="/dev/null", caption_status="pending")]),
        ])
        resp = run_pull_frame(
            _req(),
            engine,
            continuum_client=_client_with_frame(_frame()),
            redis_client=MagicMock(),
            now_fn=now,
        )
        assert resp.status == "already_exists"
