"""
Revoke a trailer — sets is_active=false so future pushes are rejected with 403.

    .venv/bin/python scripts/revoke_trailer.py --serial YARD-A-001
"""

from __future__ import annotations

import argparse
import os
import sys

import sqlalchemy as sa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    args = ap.parse_args()

    db_url = os.environ["DATABASE_URL"]
    engine = sa.create_engine(db_url)

    with engine.connect() as c:
        result = c.execute(
            sa.text(
                "UPDATE panoptic_trailers SET is_active = false, updated_at = now() "
                "WHERE serial_number = :sn RETURNING serial_number"
            ),
            {"sn": args.serial},
        )
        row = result.fetchone()
        c.commit()

    if row is None:
        print(f"no trailer with serial {args.serial} — nothing to revoke", file=sys.stderr)
        return 1
    print(f"trailer {args.serial} revoked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
