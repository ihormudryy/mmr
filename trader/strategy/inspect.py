"""AST-based strategy scanner — static facts about Strategy subclasses.

Shared by ``mmr strategies inspect`` and the web dashboard's "available
strategies" section. Pure ``ast`` analysis: strategy files are NEVER
executed, so a broken import can't crash the caller — it degrades to a
``parse_error`` row.

Per class we report:
- ``class`` / ``file``    — identifiers for ``backtest --class`` / deploy
- ``mode``                — ``precompute`` (fast path), ``on_prices``
                            (legacy, O(N²) in backtests), ``inherited``
- ``tunables``            — upper-case class attributes with literal
                            defaults, plus ``self.params.get('key', default)``
                            knobs found in method bodies
- ``docstring``           — first line; ``docstring_full`` — the whole thing
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Union

_NON_LITERAL = object()


def _literal(node) -> Any:
    """Literal value for an AST node, or ``_NON_LITERAL`` — tunables whose
    defaults are expressions are computed at class scope, not user-tunable."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _NON_LITERAL


def _is_tunable_name(name: str) -> bool:
    # str.isupper() is True for '_PRIVATE' too — exclude private-by-convention.
    return name.isupper() and not name.startswith('_')


def _extends_strategy(cls: ast.ClassDef) -> bool:
    return any(
        (isinstance(b, ast.Name) and b.id == 'Strategy')
        or (isinstance(b, ast.Attribute) and b.attr == 'Strategy')
        for b in cls.bases
    )


def _scan_class(cls: ast.ClassDef, filename: str) -> Dict[str, Any]:
    tunables: Dict[str, Any] = {}
    methods: set = set()
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and _is_tunable_name(target.id):
                    val = _literal(item.value)
                    if val is not _NON_LITERAL:
                        tunables[target.id] = val
        elif isinstance(item, ast.AnnAssign):
            # e.g. ``EMA_PERIOD: int = 20``
            if (isinstance(item.target, ast.Name)
                    and _is_tunable_name(item.target.id)
                    and item.value is not None):
                val = _literal(item.value)
                if val is not _NON_LITERAL:
                    tunables[item.target.id] = val
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.add(item.name)
            # Older strategies keep tunables in ``self.params`` and read them
            # via ``self.params.get('key', default)`` — surface those too.
            for node in ast.walk(item):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == 'get'
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == 'params'
                        and isinstance(func.value.value, ast.Name)
                        and func.value.value.id == 'self'
                        and node.args):
                    key_node = node.args[0]
                    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                        default = _literal(node.args[1]) if len(node.args) > 1 else None
                        if default is _NON_LITERAL:
                            default = None
                        # First-seen default wins if the key repeats.
                        tunables.setdefault(key_node.value, default)

    if 'precompute' in methods and 'on_bar' in methods:
        mode = 'precompute'
    elif 'precompute' in methods:
        # precompute without on_bar — falls back to the default on_bar that
        # calls on_prices. Rare, but valid.
        mode = 'precompute+on_prices'
    elif 'on_prices' in methods:
        mode = 'on_prices'
    else:
        mode = 'inherited'

    docstring = ast.get_docstring(cls) or ''
    return {
        'file': filename,
        'class': cls.name,
        'mode': mode,
        'tunables': tunables,
        'docstring': docstring.split('\n', 1)[0].strip(),
        'docstring_full': docstring,
    }


def scan_strategies(directory: Union[str, Path]) -> List[Dict[str, Any]]:
    """Scan ``directory`` for Strategy subclasses. Missing directory → []."""
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []

    rows: List[Dict[str, Any]] = []
    for py in sorted(p for p in directory.glob('*.py') if not p.name.startswith('_')):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError as ex:
            rows.append({
                'file': py.name,
                'class': '',
                'mode': 'parse_error',
                'tunables': {},
                'docstring': f'syntax error: {ex.msg}',
                'docstring_full': f'syntax error: {ex.msg}',
            })
            continue
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            if _extends_strategy(cls):
                rows.append(_scan_class(cls, py.name))
    return rows
