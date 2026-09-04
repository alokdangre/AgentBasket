from __future__ import annotations

import logging
import signal
import threading

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import DomainError
from app.payments.razorpay import (
    RazorpayRecurringGateway,
    get_razorpay_recurring_gateway,
)
from app.schemas.scheduled_purchase import ScheduledWorkerResultOut
from app.services.scheduled_execution import ScheduledPurchaseExecutor

logger = logging.getLogger("agentbasket.scheduled-purchases")
shutdown = threading.Event()


def run_once(gateway: RazorpayRecurringGateway | None = None) -> ScheduledWorkerResultOut:
    """Run one durable claim/execute cycle using a fresh database session."""
    recurring_gateway = gateway or get_razorpay_recurring_gateway()
    with SessionLocal() as db:
        return ScheduledPurchaseExecutor(db).run_cycle(recurring_gateway)


def _request_shutdown(signum: int, _frame: object) -> None:
    logger.info("Scheduled-purchase worker received signal %s; stopping.", signum)
    shutdown.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    if not settings.razorpay_recurring_enabled:
        logger.warning(
            "Scheduled-purchase worker is idle because RAZORPAY_RECURRING_ENABLED is false."
        )
        shutdown.wait()
        return

    gateway = get_razorpay_recurring_gateway()
    logger.info(
        "Scheduled-purchase worker started (poll=%ss, batch=%s).",
        settings.scheduled_worker_poll_seconds,
        settings.scheduled_worker_batch_size,
    )
    while not shutdown.is_set():
        try:
            result = run_once(gateway)
            if result.claimed or result.processed:
                logger.info("Scheduled-purchase cycle: %s", result.model_dump(mode="json"))
        except DomainError as error:
            logger.error("Scheduled-purchase cycle rejected [%s]: %s", error.code, error.message)
        except Exception:
            logger.exception("Unexpected scheduled-purchase worker failure.")
        shutdown.wait(settings.scheduled_worker_poll_seconds)

    logger.info("Scheduled-purchase worker stopped.")


if __name__ == "__main__":
    main()
