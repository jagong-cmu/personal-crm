"""Polling workers (T13).

Periodically pulls each OAuth source using its persisted incremental cursor
(historyId / syncToken in sync_state), so steady-state syncs are cheap deltas. Push
notifications (Gmail Pub/Sub, Calendar watch channels) are DEFERRED by decision —
polling is the v1 freshness mechanism.

Run once (e.g. cron):      python -m app.workers.poll --once
Run the loop (foreground): python -m app.workers.poll

Each source runs in its own tenant-scoped transaction; one source failing does not
stop the others (the per-record dead-letter from T7 still applies within a source).
"""
from __future__ import annotations

import argparse
import logging
import time

from app.config import get_settings
from app.db.session import SessionLocal, set_tenant
from app.services.connectors import calendar, contacts, gmail

logger = logging.getLogger(__name__)

# contacts has no incremental cursor wired yet (full each run); gmail/calendar are deltas.
_SOURCES = {"contacts": contacts.sync, "gmail": gmail.sync, "calendar": calendar.sync}
_DEFAULT_INTERVAL = 300  # seconds


def run_once(tenant_id=None) -> dict:
    """Sync every source once for the tenant. Returns {source: result|error-string}."""
    tenant_id = tenant_id or get_settings().tenant_uuid
    summary: dict[str, object] = {}
    for name, fn in _SOURCES.items():
        db = SessionLocal()
        try:
            set_tenant(db, tenant_id)
            result = fn(db, tenant_id)
            summary[name] = result
            logger.info("sync %s ok: %s", name, result)
        except Exception as exc:  # noqa: BLE001 — isolate per-source failures
            summary[name] = f"{type(exc).__name__}: {exc}"
            logger.warning("sync %s failed: %s", name, exc)
        finally:
            db.close()
    return summary


def run_loop(interval: int = _DEFAULT_INTERVAL, tenant_id=None) -> None:  # pragma: no cover
    logger.info("poll loop starting (interval=%ss)", interval)
    while True:
        run_once(tenant_id)
        time.sleep(interval)


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Network Intelligence polling worker")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    parser.add_argument("--interval", type=int, default=_DEFAULT_INTERVAL)
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        run_loop(args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
