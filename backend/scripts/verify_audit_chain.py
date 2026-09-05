from __future__ import annotations

import argparse
import uuid

from sqlalchemy import select

from app.core.database import SessionLocal
from app.db.models import Merchant
from app.services.audit import AuditIntegrityService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify one AgentBasket audit hash chain.")
    parser.add_argument("--merchant", default="ember-and-leaf", help="Merchant slug")
    parser.add_argument("--aggregate-type", required=True)
    parser.add_argument("--aggregate-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with SessionLocal() as db:
        merchant_id = db.scalar(select(Merchant.id).where(Merchant.slug == args.merchant))
        if merchant_id is None:
            print(f"FAIL merchant_not_found slug={args.merchant}")
            return 2
        result = AuditIntegrityService(db).verify_stream(
            uuid.UUID(str(merchant_id)),
            args.aggregate_type,
            args.aggregate_id,
        )
        if not result.valid:
            print(
                "FAIL "
                f"events={result.event_count} reason={result.reason} "
                f"event_id={result.first_invalid_event_id}"
            )
            return 1
        print(f"PASS events={result.event_count} hash_algorithm=SHA-256")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
