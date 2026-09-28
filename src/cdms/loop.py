"""Event loop factory for running uvicorn on Windows.

Async psycopg cannot use the ProactorEventLoop that uvicorn picks on Windows in single-process mode, so
local runs pass `--loop cdms.loop:selector_loop`. Linux containers do not need it.
"""

import asyncio
import selectors


def selector_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())
