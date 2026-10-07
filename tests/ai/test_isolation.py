"""The ai container holds model credentials only. Importing the package must not pull in the
trading runtime, the broker library or the market-data providers."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("ib_async", "trader.trading", "trader.trader_service", "trader.data_providers",
             "trader.messaging", "trader.container", "alpaca")


def test_importing_the_ai_package_stays_clear_of_the_trading_runtime():
    code = (
        "import sys, importlib\n"
        "for name in ('clock','config','model_client','schema','store','journal','budget','gateway','replay','untrusted'):\n"
        "    importlib.import_module('trader.ai.' + name)\n"
        f"bad = [m for m in sys.modules if m.startswith({FORBIDDEN!r})]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
