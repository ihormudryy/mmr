"""The runtime modules may use typed RPC and the XNYS calendar, never the trading runtime or market data."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("ib_async", "trader.trading", "trader.trader_service", "trader.data_providers", "trader.scoreboard",
             "trader.strategy", "trader.simulation", "alpaca",
             # SP3 Plan 5 (G14): no authored-strategy store and no sandbox in the ai service
             "trader.sandbox", "trader.automation.authored")
# SP2c Plan 4: the research menu reads strategy files as text through the AST scanner, nothing else.
# SP3 Plan 5 (G1/G2): the author runs the trader's pure submission check and reads its refusal codes; the
# authored store, files, isolation, schema, config and wire modules stay forbidden.
ALLOWED = ("trader.strategy", "trader.strategy.inspect",
           "trader.automation.authored_submission", "trader.automation.authored_refusals")
MODULES = ("trader.ai.ids", "trader.ai.runtime_schema", "trader.ai.rpc_clients", "trader.ai.leadership",
           "trader.ai.schedule", "trader.ai.engine", "trader.ai.submitter", "trader.ai.outbox",
           "trader.ai.signal_intake", "trader.ai.controller", "trader.ai_service",
           # SP2 Plan 6
           "trader.ai.decision_schema", "trader.ai.tools", "trader.ai.evidence", "trader.ai.discovery_client",
           "trader.ai.roles", "trader.ai.baselines", "trader.ai.decision_engine", "trader.ai.decision_replay",
           # SP2c Plan 4
           "trader.ai.research_cycle", "trader.ai.research_menu", "trader.ai.research_roles",
           "trader.ai.research_wire", "trader.ai.backtest_judge",
           # SP3 Plan 5
           "trader.ai.author_schema", "trader.ai.author_wire", "trader.ai.author_roles", "trader.ai.author_facts",
           "trader.ai.author_loop", "trader.ai.authored_offers")


def test_the_runtime_stays_clear_of_the_trading_runtime():
    code = ("import sys, importlib\n"
            f"for name in {MODULES!r}:\n    importlib.import_module(name)\n"
            f"print(','.join(sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}) "
            f"and m not in {ALLOWED!r})))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
