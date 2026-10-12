import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "trader"


def test_no_production_module_imports_the_tests_package():
    offenders = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [(path, a.name) for a in node.names if a.name.split(".")[0] == "tests"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tests":
                offenders.append((path, node.module))
    assert not offenders, offenders
