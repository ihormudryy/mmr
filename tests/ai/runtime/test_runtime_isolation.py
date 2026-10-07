"""The runtime modules may use typed RPC and the XNYS calendar, never the trading runtime or market data."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("ib_async", "trader.trading", "trader.trader_service", "trader.data_providers", "trader.scoreboard",
             "trader.strategy", "alpaca")
MODULES = ("trader.ai.ids", "trader.ai.runtime_schema", "trader.ai.rpc_clients", "trader.ai.leadership",
           "trader.ai.schedule", "trader.ai.engine", "trader.ai.submitter", "trader.ai.outbox",
           "trader.ai.signal_intake", "trader.ai.controller", "trader.ai_service",
           # SP2 Plan 6
           "trader.ai.decision_schema", "trader.ai.tools", "trader.ai.evidence", "trader.ai.discovery_client",
           "trader.ai.roles", "trader.ai.baselines", "trader.ai.decision_engine")


def test_the_runtime_stays_clear_of_the_trading_runtime():
    code = ("import sys, importlib\n"
            f"for name in {MODULES!r}:\n    importlib.import_module(name)\n"
            f"print(','.join(sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}))))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
