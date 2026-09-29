"""Emulator of the Vietful Inventory Service (decision D12 in docs/assumptions.md).

Same source and database as CDMS, but a separate process (`uvicorn cdms.emulator.main:app --port 8101`) and
a separate PostgreSQL schema `vietful`. CDMS code never imports this package and never reads `vietful.*`: it
talks to the emulator over HTTP only (tests/unit/test_boundaries.py), so swapping in the real Vietful is a URL
change.
"""
