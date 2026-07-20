"""Test/drill-only harness helpers (P4 Task 4).

Nothing under ``trader.testing`` is imported by any production service
(``trader_service``, ``strategy_service``, ``data_service``, ``web.app``).
It exists purely for synthetic fault-injection drills and their pytest
integration tests — see ``trader.testing.faults``.
"""
