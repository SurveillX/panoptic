"""
Register a trailer in the known-trailer registry (panoptic_trailers).

    .venv/bin/python scripts/add_trailer.py --serial YARD-A-001 --name "Yard A #1"
    .venv/bin/python scripts/add_trailer.py --serial YARD-A-001            # idempotent upsert

Rules:
  - Upsert semantics: existing rows are updated (is_active reset to true).
  - --inactive flag creates/updates in an inactive state.
"""

from __future__ import annotations

import argparse
import os
import sys

import sqlalchemy as sa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="trailer serial number")
    ap.add_argument("--name", default=None, help="human-readable label")
    ap.add_argument("--notes", default=None)
    ap.add_argument(
        "--inactive",
        action="store_true",
        help="create/update with is_active=false (rejects new pushes until re-enabled)",
    )
    args = ap.parse_args()

    db_url = os.environ["DATABASE_URL"]
    engine = sa.create_engine(db_url)

    is_active = not args.inactive
    with engine.connect() as c:
        c.execute(
            sa.text(
                """
                INSERT INTO panoptic_trailers (serial_number, name, is_active, notes)
                VALUES (:sn, :name, :is_active, :notes)
                ON CONFLICT (serial_number)
                  DO UPDATE SET
                    name        = COALESCE(EXCLUDED.name, panoptic_trailers.name),
                    is_active   = EXCLUDED.is_active,
                    notes       = COALESCE(EXCLUDED.notes, panoptic_trailers.notes),
                    updated_at  = now()
                """
            ),
            {"sn": args.serial, "name": args.name, "is_active": is_active, "notes": args.notes},
        )
        c.commit()

    print(f"trailer {args.serial} {'active' if is_active else 'inactive'} (name={args.name!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
