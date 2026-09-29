"""Crash hook of the failure scenarios (docs/testing.md F1 / F2): enabled only by `FAULT_*` settings."""

import logging
import os
import sys

log = logging.getLogger(__name__)


def crash(reason: str) -> None:
    """Die like a killed process: no cleanup, no exception handlers, no transaction commit / rollback."""
    log.critical("simulated crash: %s", reason)
    sys.stderr.flush()
    os._exit(3)
