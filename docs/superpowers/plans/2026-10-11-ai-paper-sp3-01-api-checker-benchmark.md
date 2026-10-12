# AI Paper SP3 — Plan 1: Authored-strategy API, static checker, escape corpus and benchmark — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-Python foundation every later SP3 plan reuses — the `AuthoredStrategy` API, authored keys and digests, the two-list static checker with its escape corpus and a shipped-pandas/numpy walk, a faithful ORB port fixture — and a benchmark that measures the cost of a streamed one-bar-at-a-time evaluation so the owner can rule on evaluation length.

**Architecture:** A new package `trader/authored/` holds `api.py` (the `AuthoredStrategy` base class and `Decision`), `keys.py` (digests and authored keys), `allowed_api.py` (the allow-lists and deny-list as data) and `static_check.py` (an AST walk, no execution). Nothing here runs authored code in-process except the benchmark's throwaway child, which forks and drops nothing (it only measures the pipe protocol's cost). The checker is the second layer of SP3's defence; Plan 2's process boundary is the real isolation, so this plan's checker never claims to be a sandbox.

**Tech Stack:** Python 3.12.13, the standard library `ast`/`hashlib`/`struct`/`json`/`multiprocessing`, pandas 3.0.6 and numpy 2.5.3 (already pinned in `uv.lock`), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md`, sections 7.1, 7.2, 9 and 10 (escape corpus, static check, shipped-library walk, benchmark). Index: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md` (rule R0 names this plan owns).

## Global Constraints

- **Base:** master at `d69fdcf2` (`fix: hand on_bar only the bars up to the current one in backtests (#151)`). Code read at `/Users/mudryy/private/mmr`; worktree `/private/tmp/sp3-plans` on branch `docs/sp3-plans`.
- **Shipped versions, copied from `uv.lock`:** numpy `2.5.3`, pandas `3.0.6`, Python `3.12.13`. The walk test asserts against the installed versions, not these literals, but every allow-list name must resolve on them.
- **Package names (rule R0, index), used by Plans 2–5 exactly:** `trader/authored/api.py` (`AuthoredStrategy`, `Decision`), `trader/authored/keys.py` (`source_digest`, `authored_key`, `parse_authored_key`, `is_authored_key`), `trader/authored/allowed_api.py` (`ALLOWED_IMPORTS`, `ALLOWED_FROM_IMPORTS`, `ALLOWED_FROM_IMPORT_NAMES`, `ALLOWED_MODULE_ATTRIBUTES`, `ALLOWED_BUILTINS`, `ALLOWED_AST_NODES`, `DENIED_CHAIN_NAMES`, `DENIED_CHAIN_PREFIXES`, `BANNED_NAMES`, `BANNED_ATTRIBUTES`, `IO_ATTRIBUTE_PREFIXES`, `IO_EXEMPT_NAMES`, `IO_NAMES`, `PROTECTED_NAMES`; rulings 11-27), `trader/authored/static_check.py` (`StaticViolation`, `check_source`, `AuthoredStaticRejected`, `require_clean`).
- **API shape, copied from the spec (7.1) and index verbatim:** `AuthoredStrategy.LOOKBACK_BARS: int = 200` (valid range 1..2000); `on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None`; `Decision` is a frozen dataclass with `action: Literal["BUY", "SELL"]` and `probability: float` (0..1). Single symbol, long only. No storage, runtime, logger, clock, randomness or conid lookup.
- **Refusal code:** every static-check failure is reported as `AUTHORED_STATIC_REJECTED` with the exact rule name and line (spec 7.2). The checker never raises on bad authored input except through `AuthoredStaticRejected`; it returns a list.
- **Digests:** a digest is the lowercase hex sha256 of the file bytes, prefixed `sha256:` (spec section 3). An authored key is `authored:sha256:<hex>:<Class>` (index, spec 3).
- **No migrations, no RPC, no config, no container** in this plan (index migration table: Plan 1 has none).
- **DuckDB / secrets:** no database access and no secret handling in this plan; the usual rules still bind if one is ever added (they are not here).
- **Escape corpus files are data, never imported.** They live under `tests/authored/escapes/`, are read as text, and must not match pytest's `test_*.py` collection pattern (`pyproject.toml`). `tests/authored/escapes/` has no `__init__.py`.
- Test-first. Per task run only the listed tests. Full suite once, in the last task, under the shared lock `/private/tmp/mmr-suite.lock`, `-n 8 --timeout=120`.
- Commit subjects `feat:` / `test:` / `chore:`, lowercase, imperative, one line. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No push, no PR, no merge, no container restart, no network call is authorized by this plan.

## Rulings

Decisions the spec or index does not settle. Add any new one here as `Plan 1 ruling K: ...`, one line, and put the measured benchmark result in the index's "Owner rulings to confirm" (Task 7).

1. **Plan 1 ruling 1 (the benchmark result; spec section 14 amendment).** Plan 1 ruling 1: a full authored evaluation (10 conids x 690 sessions x 3 cohort points, 2 tunables, 2 neighbours each, 6 folds, embargo 5, + 90-session holdout) is estimated at 10.57 h at an upper-bound combined cost of 0.8872 ms/bar (streamed + in-process backtest, strategy time counted twice). Since 10.57 h is over 4 h: reduce research_service.period_sessions for authored keys only to N=310 (>= 132: 6 folds, embargo 5, holdout 90); never weaken the sandbox. Measured 2026-10-12 (`scripts/sp3_benchmark.py --conids 1 --sessions 40 --runs 3`: streamed 0.5376 ms/bar median of 3, 40 signals fired; in-process backtest 0.3496 ms/bar; 42,880,500 bar sends at full scale). The script proposes N=310; the owner confirmed N=302 (2026-10-11) and that decision stands. 302 is above the 132 floor, so the ruling is unchanged. At the selected configuration (302 sessions, holdout scaled to 39; otherwise as above) the same upper bound of 0.8872 ms/bar gives about 4.6 h (4.64 h, 18,813,600 bar sends; `selected_configuration_text` in `scripts/sp3_benchmark.py`, same arithmetic as `extrapolate`). That is above the 4 h planning line; **the owner accepted it (2026-10-12)**, as it fits the ~15 h overnight window. **Owner confirmed 302 sessions (2026-10-11);** Plan 4 Task 12 builds it (`research_service.authored_evaluation_sessions`).
2. **Plan 1 ruling 2: the benchmark needs `api.py` and the ORB port, so those are built before it.** The spec calls the benchmark the first plan task, but a streamed evaluation of an `AuthoredStrategy` ORB port cannot run before `AuthoredStrategy` and the port exist. This plan builds `api.py` and `keys.py` (Task 1), the ORB fixture (Task 2), then the benchmark (Task 3). The benchmark is still the plan's first *measurement* deliverable and its ruling is recorded before Plans 4/5 size any evaluation. *Cost if wrong: none; purely ordering.*
3. **Plan 1 ruling 3: the ORB port hardcodes `America/New_York` and drops `SESSION_TZ`.** Authored tunables are numbers only (spec 7.3, index), and `SESSION_TZ` is a string. The port converts the UTC index with a literal `"America/New_York"`. *Cost if wrong: an authored ORB cannot run on a non-US exchange; SP3 is US-paper only (spec 12, out of scope: multi-symbol/other).* 
4. **Plan 1 ruling 4: the ORB port emits only the two breakout cross signals; it carries no `close_by_time`, `risk` or EOD-flat.** `Decision` has only `action` and `probability` (spec 7.1). End-of-day flatness and sizing are not the strategy's job: in a backtest `PaperAutomationRules.flatten_due` flattens near the close (`trader/simulation/live_rules.py`, 15 min before close via `calendar_policy`), and live the risk gate sizes and the exit path manages stops (spec 7.1: "Jev and the risk gate size the trade"). The Plan 4 equivalence test compares the BUY/SELL cross stream only. *Cost if wrong: a divergence from the catalogue ORB's EOD SELL is expected and documented, not a bug.*
5. **Plan 1 ruling 5: `from numpy|pandas|math|statistics import name` is refused; those modules are reachable only as `import numpy as np` etc.** The module-attribute allow-list (spec 7.2) only governs `alias.attr` chains; a bare imported name (`from numpy import mean`) would bypass it. Allowed from-imports are only `from typing import ...`, `from dataclasses import ...` and `from trader.authored.api import ...`. Dotted imports (`import pandas.io`) and `import *` are refused. *Cost if wrong: an author must write `np.mean`, not `mean`; the ORB port already does.*
6. **Plan 1 ruling 6: the module-attribute allow-list and the deny-list extend the spec's lists (the spec says "in addition to").** Added to `DENIED_CHAIN_NAMES`: `ctypes`, `dump`, `dumps`, `loads`, `frombuffer` (numpy's `ndarray.dumps`/`ndarray.ctypes` pickle or expose C memory; `np.frombuffer`/`np.load`/`pickle.loads` reconstruct objects). The `statistics` and `math` allow-lists list only functions, never the submodules those modules expose (`statistics.random`, `statistics.sys`, `statistics.math`, `statistics.numbers` are modules on the shipped build — the walk test proves none is listed). *Cost if wrong: a named-but-unverified attribute makes the walk test fail loudly, which is the safe side.*
7. **Plan 1 ruling 7: a module alias may appear only as the leftmost `Name` of an attribute chain.** `x = np` then `x.<anything>`, passing `np` as an argument, subscripting it, or returning it — all refused (`module_alias_misuse`). This closes alias rebinding that would smuggle a module past the "first attribute must be allow-listed" rule. *Cost if wrong: a benign `arr = np.array(...)` is unaffected (the alias is still the chain root); only naked-alias uses are refused.*
8. **Plan 1 ruling 8: every string literal is checked, docstrings included.** A literal is refused (`string_literal`) when it contains `__`, equals a denied or banned name, or matches `read_*` / `to_*` (except the exact word `to_numpy`). This implements the spec's "`importlib` and `__import__` in strings", `"{0.__globals__}".format(x)`, and pandas string dispatch (`df.agg("to_pickle", p)`, `df.apply("...")`). No docstring exemption keeps one rule instead of two; an authored docstring that needs `__` or a `to_`/`read_` token is vanishingly unlikely and refusal is safe. *Cost if wrong: a strategy comment cannot be a string literal containing `__`; use a `#` comment.*
9. **Plan 1 ruling 9: the benchmark's child is a throwaway measurement harness, not Plan 2's sandbox.** It forks a child, streams length-prefixed JSON bars over a pipe exactly as the spec's protocol frames them (`{"v":1,...}`), and runs the ORB port. It drops no privileges, sets no rlimits and imposes no seccomp — it exists only to measure the per-bar cost of the pipe + per-bar `history` build that Plan 2's real child must match or beat. The benchmark prints that Plan 2 must meet the measured per-bar cost. *Cost if wrong: if Plan 2's child is slower, the real evaluation is slower than this estimate; the benchmark says so.*
10. **Plan 1 ruling 10 (decision D8): the ORB port keeps the catalogue's `MIN_BARS = 40` warm-up gate** and emits nothing while `len(history) <= MIN_BARS` (the catalogue: nothing at bar index `< MIN_BARS`). With it the port gives the catalogue ORB's BUY/SELL stream bar for bar on Plan 4's three-session synthetic bars (1170 bars, one BUY and three SELLs, checked while integrating the plans), so Plan 4 uses this fixture instead of a port of its own. *Cost if wrong: Plan 4's equivalence test fails loudly.*
11. **Plan 1 ruling 11: keys use `fullmatch`, and a non-string digest, class name or key raises `ValueError`.** `$` accepts a trailing newline, and the regex raised `TypeError` on a non-string. *Cost if wrong: none (stricter).*
12. **Plan 1 ruling 12: the benchmark child replies the full `SIGNAL` frame** (`v`, `type`, `session`, `t`, `action`, `probability`), so the measured cost includes the real reply. *Cost if wrong: none.*
13. **Plan 1 ruling 13: ruling 1's per-bar figure is an upper bound.** It adds the streamed cost and the in-process backtest cost, so strategy time is counted twice; the script and ruling 1 label it "upper-bound combined cost", never "streamed". *Cost if wrong: the evaluation-length estimate is high; the owner already chose 302 sessions.*
14. **Plan 1 ruling 14: `DENIED_CHAIN_PREFIXES` (`func_`, `gi_`, `cr_`, `ag_`, `f_`, `tb_`, `co_`, `im_`) and the exact names `mro`, `register`, `boxplot`, `hist` are refused anywhere in a chain.** Cython methods expose `func_globals`/`func_code` without an underscore, and `AuthoredStrategy.register` is `ABCMeta.register`. The walk test goes two levels below every module attribute and every from-import name and fails on any module, code object, frame, traceback or module globals reached by an undenied name. *Cost if wrong: a benign name starting `f_` or `co_` is refused; the author renames it.*
15. **Plan 1 ruling 15: a from-import may bind only the names in `ALLOWED_FROM_IMPORT_NAMES[module]`,** kept in `allowed_api.py` with the other lists so Plan 2's guard reads the same data. Without it `from trader.authored.api import pd` would bind a module. *Cost if wrong: none.*
16. **Plan 1 ruling 16: stricter class shape.** Exactly one top-level class, whose only base is the unrenamed `AuthoredStrategy` imported from `trader.authored.api`, with no keywords; a nested class has no bases or keywords; `AuthoredStrategy` and `Decision` (`PROTECTED_NAMES`) are never re-bound; no defined name starts with `_` (spec 7.2). *Cost if wrong: authors cannot write `_helper`; they write `helper`.*
17. **Plan 1 ruling 17: the checker parses under a module lock with warnings ignored.** `warnings.catch_warnings` is process-global and the trader checks files on several RPC threads; under `-W error` a `SyntaxWarning` would otherwise change the verdict. *Cost if wrong: a brief warning suppression in another thread.*
18. **Plan 1 ruling 18: `from __future__` stays refused and the string rule (ruling 8) stands.** The author gets the violations and one retry. *Cost if wrong: an occasional benign file is refused.*
19. **Plan 1 ruling 19: call keyword names stay unchecked.** A keyword name performs no attribute access. *Cost if wrong: none known.*
20. **Plan 1 ruling 20: `match` is refused, builtins are an allow-list, and pandas string dispatch is denied.** Class patterns read attributes by plain-string names; every builtin outside `ALLOWED_BUILTINS` (`type`, `object`, `super`, `iter`, `next`, `print`, ...) is refused and no builtin name may be re-bound; `apply`, `agg`, `aggregate`, `transform`, `applymap`, `pipe` take a method name built at runtime. *Cost if wrong: authors lose `match` and those pandas helpers (use `rolling().mean()` etc.).*
21. **Plan 1 ruling 21: every AST node must be in `ALLOWED_AST_NODES`, type parameters are refused, and every loaded name must be bound in a visible scope or be an allowed builtin (`unknown_name`).** This fails closed on syntax a later Python adds (`TryStar`, `TypeAlias`, ...). *Cost if wrong: a harmless new syntax form is refused until it is listed.*
22. **Plan 1 ruling 22: walrus, comprehensions, f-strings, `dict(...)` and the six harmless probe-1 cases pass;** they reach only allowed names. Both probe rounds are pinned as tests. *Cost if wrong: an escape through one of these forms; the corpus and the sandbox (Plan 2) are the backstop.*
23. **Plan 1 ruling 23: the corpus test pins, per file, the rule that catches the hostile act (`EXPECTED_RULE`).** The two resource-attack files map to `None` and must fail only on their marker local. *Cost if wrong: none.*
24. **Plan 1 ruling 24: `typing` and `dataclasses` are from-import only, and a plain `import X` needs an `ALLOWED_MODULE_ATTRIBUTES` list.** On 3.12 `typing.operator.attrgetter` reaches `os.kill` (corpus `escape_typing_operator_chain.py`); ruling 5's reasoning applies. A test asserts that every module in `ALLOWED_IMPORTS` except `trader.authored.api` has an attribute list, and the checker refuses a plain import without one. *Cost if wrong: authors write `from typing import Mapping` (the ORB port already does).*
25. **Plan 1 ruling 25: a module-level assignment must have a literal value** (`ast.literal_eval` accepts it, including `-1`); anything else, and an annotation without a value, is `module_statement` (spec 7.2: constant assignments). *Cost if wrong: an author computes the value inside the class or method instead.*
26. **Plan 1 ruling 26: all checker rule data lives in `allowed_api.py`** (`BANNED_NAMES`, `BANNED_ATTRIBUTES`, `IO_ATTRIBUTE_PREFIXES`, `IO_EXEMPT_NAMES`, `IO_NAMES`, `PROTECTED_NAMES` beside the allow-lists); `static_check.py` holds only the rules. *Cost if wrong: none.*
27. **Plan 1 ruling 27: Plan 2's child import guard and reduced builtins are built from `allowed_api`** (`ALLOWED_IMPORTS`, `ALLOWED_FROM_IMPORT_NAMES`, `ALLOWED_BUILTINS`), never from wider lists of their own (Plan 2 ruling 31). *Cost if wrong: none; the checker already refuses anything the narrower runtime lists would block.*

## Review Focus

Input classes the spec implies but the happy-path tests do not exercise, most likely to bite first. Each has its pinning test placed in the owning task.

1. **A checker that is skipped on `ast.parse` failure.** A file that does not parse must be one `AUTHORED_STATIC_REJECTED` violation (rule `unparseable`), never an uncaught `SyntaxError` that a caller mistakes for "clean". → Task 5 `test_a_file_that_does_not_parse_is_one_violation`.
2. **A dunder reached without a literal underscore in source**, e.g. `getattr(x, "__class__")` or `type(x).__bases__` via a computed name. The underscore rule catches attribute/`Name` nodes; the string rule (ruling 8) and the deny of `getattr` must catch the computed path. → Task 6 corpus files `escape_computed_getattr.py`, `escape_format_globals.py`; Task 5 `test_every_escape_file_is_rejected`.
3. **A shipped library name that is actually a submodule** (`np.lib`, `pd.io`, `statistics.random`). The allow-list must never list one, and the walk test must fail if a future bump turns a listed name into a module or exposes `os`/`sys`/`subprocess`/`builtins`. → Task 4 `test_every_allowed_attribute_is_not_a_module_and_hides_no_process_module`.
4. **`LOOKBACK_BARS` out of the 1..2000 range or non-int.** The API constant is validated so a strategy cannot ask for an unbounded or zero history. → Task 1 `test_lookback_bars_out_of_range_is_refused`.
5. **A `Decision` with a probability outside 0..1 or NaN.** The dataclass must reject it at construction, so a bad model output fails loudly, not silently. → Task 1 `test_decision_rejects_a_probability_outside_the_unit_interval`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/authored/__init__.py`, `trader/authored/api.py` | `AuthoredStrategy`, `Decision` | 1 |
| `trader/authored/keys.py` | digests, authored keys | 1 |
| `tests/authored/__init__.py`, `tests/authored/fixtures/__init__.py`, `tests/authored/fixtures/orb_authored.py` | ORB port fixture (reused by Plans 2–4) | 2 |
| `scripts/sp3_benchmark.py` | streamed-evaluation benchmark + ruling text | 3 |
| `trader/authored/allowed_api.py` | allow-lists and deny-list as data | 4 |
| `trader/authored/static_check.py` | the AST checker | 5 |
| `tests/authored/escapes/*.py` | escape corpus (data, not collected) | 6 |
| `tests/authored/test_*.py` | unit tests per module | 1,2,3,4,5,6 |

Shared test helper: `tests/authored/fixtures/orb_authored.py` is imported by Tasks 2, 3, 6 and later plans.

---

### Task 1: The authored-strategy API, `Decision` and authored keys

**Files:**
- Create: `trader/authored/__init__.py`, `trader/authored/api.py`, `trader/authored/keys.py`
- Create: `tests/authored/__init__.py`, `tests/authored/test_api.py`, `tests/authored/test_keys.py`

**Interfaces:**
- Consumes: nothing (standard library, pandas types for the annotation only).
- Produces (Plans 2–5 use these exactly):
  - `AuthoredStrategy` — abstract base, class attribute `LOOKBACK_BARS: int = 200` validated 1..2000 at subclass creation, abstract `on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None`.
  - `Decision(action: Literal["BUY", "SELL"], probability: float)` — frozen dataclass, validates `action` and `probability` in `__post_init__`.
  - `source_digest(source: bytes) -> str` → `"sha256:<hex>"`.
  - `authored_key(digest: str, class_name: str) -> str` → `"authored:sha256:<hex>:<Class>"`.
  - `parse_authored_key(key: str) -> tuple[str, str]` → `(digest, class_name)`, raises `ValueError` on a bad key.
  - `is_authored_key(key: str) -> bool`.

- [ ] **Step 1: Write the failing tests** (`tests/authored/test_api.py`)

```python
"""SP3 Plan 1 Task 1: the authored-strategy API (spec 7.1)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from trader.authored.api import AuthoredStrategy, Decision


def test_decision_holds_action_and_probability():
    d = Decision(action="BUY", probability=0.6)
    assert (d.action, d.probability) == ("BUY", 0.6)


def test_decision_is_frozen():
    d = Decision(action="SELL", probability=0.1)
    with pytest.raises(Exception):
        d.action = "BUY"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_decision_rejects_a_probability_outside_the_unit_interval(bad):
    with pytest.raises(ValueError):
        Decision(action="BUY", probability=bad)


def test_decision_rejects_an_unknown_action():
    with pytest.raises(ValueError):
        Decision(action="HOLD", probability=0.5)  # type: ignore[arg-type]


def test_default_lookback_is_200():
    class S(AuthoredStrategy):
        def on_bar(self, history, params):
            return None

    assert S.LOOKBACK_BARS == 200


@pytest.mark.parametrize("bad", [0, -1, 2001, 1.5, True])
def test_lookback_bars_out_of_range_is_refused(bad):
    with pytest.raises(ValueError):
        class S(AuthoredStrategy):
            LOOKBACK_BARS = bad

            def on_bar(self, history, params):
                return None


def test_subclass_runs_on_bar():
    class S(AuthoredStrategy):
        LOOKBACK_BARS = 3

        def on_bar(self, history, params):
            return Decision(action="BUY", probability=0.9) if len(history) >= 2 else None

    frame = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
         "close": [1.0, 2.0], "volume": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2024-01-02T14:30:00Z", "2024-01-02T14:31:00Z"], name="date"),
    )
    assert S().on_bar(frame, {}).action == "BUY"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/authored/test_api.py -q`
Expected: FAIL (`ModuleNotFoundError: trader.authored.api`).

- [ ] **Step 3: Write `trader/authored/__init__.py` and `trader/authored/api.py`**

`trader/authored/__init__.py`:

```python
"""Authored-strategy foundation for SP3 (spec 7.1, 7.2)."""
```

`trader/authored/api.py`:

```python
"""The only repo module an authored strategy may import (spec 7.1).

An authored strategy sees one symbol's recent bars and returns a long-only
decision. It gets no storage, runtime, logger, clock, randomness or conid
lookup. Jev and the risk gate size the trade, as for catalogue signals.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Mapping

import pandas as pd

MIN_LOOKBACK_BARS = 1
MAX_LOOKBACK_BARS = 2000
ACTIONS = ("BUY", "SELL")


@dataclass(frozen=True)
class Decision:
    action: Literal["BUY", "SELL"]
    probability: float

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {self.action!r}")
        probability = self.probability
        if not isinstance(probability, (int, float)) or isinstance(probability, bool):
            raise ValueError(f"probability must be a real number, got {probability!r}")
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"probability must be within 0..1, got {probability!r}")


class AuthoredStrategy(ABC):
    LOOKBACK_BARS: int = 200

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        lookback = cls.LOOKBACK_BARS
        if isinstance(lookback, bool) or not isinstance(lookback, int):
            raise ValueError(f"LOOKBACK_BARS must be an int, got {lookback!r}")
        if not MIN_LOOKBACK_BARS <= lookback <= MAX_LOOKBACK_BARS:
            raise ValueError(
                f"LOOKBACK_BARS must be within {MIN_LOOKBACK_BARS}..{MAX_LOOKBACK_BARS}, "
                f"got {lookback}")

    @abstractmethod
    def on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None:
        """Decide on the bar at ``history.index[-1]``.

        ``history`` holds the last ``LOOKBACK_BARS`` completed bars (columns
        open, high, low, close, volume; DatetimeIndex). ``params`` carries the
        tunables' values. Return a BUY or SELL ``Decision`` or ``None``.
        """
        raise NotImplementedError
```

- [ ] **Step 4: Run the API tests to verify they pass**

Run: `uv run --frozen pytest tests/authored/test_api.py -q`
Expected: PASS. Create `tests/authored/__init__.py` as an empty file so pytest treats `tests/authored` as a package (`pyproject.toml` `pythonpath`).

- [ ] **Step 5: Write the failing tests** (`tests/authored/test_keys.py`)

```python
"""SP3 Plan 1 Task 1: authored keys and digests (spec section 3)."""
from __future__ import annotations

import hashlib

import pytest

from trader.authored.keys import (
    authored_key, is_authored_key, parse_authored_key, source_digest,
)

SOURCE = b"class X(AuthoredStrategy):\n    pass\n"
HEX = hashlib.sha256(SOURCE).hexdigest()


def test_source_digest_is_prefixed_sha256_hex():
    assert source_digest(SOURCE) == f"sha256:{HEX}"


def test_authored_key_round_trips():
    key = authored_key(f"sha256:{HEX}", "OpeningRangeBreakout")
    assert key == f"authored:sha256:{HEX}:OpeningRangeBreakout"
    assert parse_authored_key(key) == (f"sha256:{HEX}", "OpeningRangeBreakout")
    assert is_authored_key(key)


def test_authored_key_refuses_a_bad_digest():
    with pytest.raises(ValueError):
        authored_key("md5:" + HEX, "X")


def test_authored_key_refuses_a_bad_class_name():
    with pytest.raises(ValueError):
        authored_key(f"sha256:{HEX}", "1Bad")


def test_parse_refuses_a_non_authored_key():
    with pytest.raises(ValueError):
        parse_authored_key("strategies/orb.py:OpeningRangeBreakout")


def test_is_authored_key_is_false_for_a_catalogue_key():
    assert not is_authored_key("strategies/orb.py:OpeningRangeBreakout")
    assert not is_authored_key(f"authored:md5:{HEX}:X")
```

- [ ] **Step 6: Run the key tests to verify they fail**

Run: `uv run --frozen pytest tests/authored/test_keys.py -q`
Expected: FAIL (`ModuleNotFoundError: trader.authored.keys`).

- [ ] **Step 7: Write `trader/authored/keys.py`**

```python
"""Content-addressed digests and authored strategy keys (spec section 3).

A digest is ``sha256:<hex>`` of the file bytes, the store key and the binding
key. An authored key is ``authored:sha256:<hex>:<Class>``.
"""
from __future__ import annotations

import hashlib
import re

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_AUTHORED_KEY = re.compile(
    r"^authored:(sha256:[0-9a-f]{64}):([A-Za-z_][A-Za-z0-9_]{0,63})$")


def source_digest(source: bytes) -> str:
    if not isinstance(source, (bytes, bytearray)):
        raise TypeError(f"source must be bytes, got {type(source).__name__}")
    return f"sha256:{hashlib.sha256(source).hexdigest()}"


def authored_key(digest: str, class_name: str) -> str:
    if not _DIGEST.match(digest):
        raise ValueError(f"digest must match sha256:<64 hex>, got {digest!r}")
    if not _CLASS_NAME.match(class_name):
        raise ValueError(f"class name is not a Python identifier: {class_name!r}")
    return f"authored:{digest}:{class_name}"


def parse_authored_key(key: str) -> tuple[str, str]:
    match = _AUTHORED_KEY.match(key)
    if match is None:
        raise ValueError(f"not an authored key: {key!r}")
    return match.group(1), match.group(2)


def is_authored_key(key: str) -> bool:
    return isinstance(key, str) and _AUTHORED_KEY.match(key) is not None
```

- [ ] **Step 8: Run both test files to verify they pass**

Run: `uv run --frozen pytest tests/authored/test_api.py tests/authored/test_keys.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add trader/authored/__init__.py trader/authored/api.py trader/authored/keys.py \
        tests/authored/__init__.py tests/authored/test_api.py tests/authored/test_keys.py
git commit -m "feat: add authored-strategy api, decision and keys"
```

---

### Task 2: The ORB port fixture

**Files:**
- Create: `tests/authored/fixtures/__init__.py`, `tests/authored/fixtures/orb_authored.py`
- Create: `tests/authored/test_orb_authored.py`

**Interfaces:**
- Consumes: `AuthoredStrategy`, `Decision` (Task 1).
- Produces: `tests/authored/fixtures/orb_authored.py` with class `OpeningRangeBreakout(AuthoredStrategy)` and the tunable constants `RANGE_MINUTES`, `VOLUME_MULT`, `RTH_OPEN_MIN`, `RTH_CLOSE_MIN`, `VOLUME_SMA_BARS`, `MIN_BARS` (ruling 10). Reused by Task 3 (benchmark), Task 6 (checker-passes test) and Plans 2–4 (Plan 4's equivalence test uses this fixture, not a port of its own: decision D8).

Read before writing: `strategies/opening_range_breakout.py` (the catalogue ORB being ported) and `trader/authored/api.py` (Task 1). Port faithfully within rulings 3 and 4: single symbol, one `on_bar`, no `precompute`, no `SESSION_TZ` string tunable, no `close_by_time`. Keep the catalogue's `MIN_BARS = 40` warm-up gate (ruling 10): the catalogue emits nothing at bar index `< MIN_BARS`, so the port emits nothing while `len(history) <= MIN_BARS`.

- [ ] **Step 1: Write the failing test** (`tests/authored/test_orb_authored.py`)

```python
"""SP3 Plan 1 Task 2: the ORB AuthoredStrategy port (spec 10 equivalence, ruling 3,4)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.authored.api import Decision
from tests.authored.fixtures.orb_authored import OpeningRangeBreakout

PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}


def _session(n=120, breakout_at=80, base=100.0):
    """One regular session of 1-min UTC bars, flat open range then a volume-backed break up."""
    start = pd.Timestamp("2024-01-02 14:30:00", tz="UTC")  # 09:30 ET
    index = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = np.full(n, base)
    close[breakout_at:] = base + 2.0          # break above the 09:30-10:00 range high
    volume = np.full(n, 1000.0)
    volume[breakout_at] = 10_000.0            # the breakout bar carries volume
    frame = pd.DataFrame(
        {"open": close, "high": close + 0.25, "low": close - 0.25,
         "close": close, "volume": volume}, index=index)
    frame.index.name = "date"
    return frame


def test_lookback_covers_a_session_plus_the_volume_window():
    assert OpeningRangeBreakout.LOOKBACK_BARS >= 390 + 20


def test_no_signal_until_more_than_min_bars_bars_are_seen():
    # A breakout at bar 30 (10:00 ET) would signal without the gate; the catalogue's MIN_BARS = 40 keeps it quiet.
    frame = _session(breakout_at=30)
    strategy = OpeningRangeBreakout()
    assert OpeningRangeBreakout.MIN_BARS == 40
    assert strategy.on_bar(frame.iloc[:31], PARAMS) is None
    assert strategy.on_bar(frame.iloc[:41], PARAMS) is None          # len 41 > MIN_BARS but no cross at bar 40


def test_no_signal_before_the_opening_range_closes():
    frame = _session()
    strategy = OpeningRangeBreakout()
    # 25 minutes in (bar 24 is 09:54 ET): the 30-minute range has not closed yet.
    assert strategy.on_bar(frame.iloc[:25], PARAMS) is None


def test_breakout_bar_is_a_buy():
    frame = _session(breakout_at=80)
    strategy = OpeningRangeBreakout()
    decision = strategy.on_bar(frame.iloc[: 81], PARAMS)
    assert isinstance(decision, Decision) and decision.action == "BUY"


def test_a_quiet_bar_inside_the_range_is_none():
    frame = _session(breakout_at=80)
    strategy = OpeningRangeBreakout()
    assert strategy.on_bar(frame.iloc[: 60], PARAMS) is None


def test_break_below_the_range_low_is_a_sell():
    frame = _session(breakout_at=80)
    low = frame.copy()
    # .loc, not chained .iloc assignment: pandas 3.0 Copy-on-Write makes the chained
    # form a no-op on a view.
    low.loc[low.index[80:], "close"] = 97.0  # drop below the range low, no volume gate on exit
    low.loc[low.index[80:], "low"] = 96.0
    strategy = OpeningRangeBreakout()
    decision = strategy.on_bar(low.iloc[: 81], PARAMS)
    assert isinstance(decision, Decision) and decision.action == "SELL"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest tests/authored/test_orb_authored.py -q`
Expected: FAIL (`ModuleNotFoundError: tests.authored.fixtures.orb_authored`).

- [ ] **Step 3: Write `tests/authored/fixtures/__init__.py` (empty) and `tests/authored/fixtures/orb_authored.py`**

```python
"""An Opening Range Breakout port as an AuthoredStrategy (SP3 Plan 1 Task 2).

Faithful to strategies/opening_range_breakout.py within the SP3 API: one
symbol, one on_bar, no precompute, US regular hours only (ruling 3), two
breakout cross signals only (ruling 4). It passes trader.authored.static_check
(Task 6). Reused by the benchmark and by Plans 2-4.
"""
from typing import Mapping

import numpy as np
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class OpeningRangeBreakout(AuthoredStrategy):
    LOOKBACK_BARS = 420

    RANGE_MINUTES = 30
    VOLUME_MULT = 1.5
    RTH_OPEN_MIN = 570
    RTH_CLOSE_MIN = 960
    VOLUME_SMA_BARS = 20
    MIN_BARS = 40

    def on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None:
        range_minutes = int(params["RANGE_MINUTES"])
        volume_mult = float(params["VOLUME_MULT"])
        rth_open = int(params["RTH_OPEN_MIN"])
        rth_close = int(params["RTH_CLOSE_MIN"])
        sma_bars = int(params["VOLUME_SMA_BARS"])
        if len(history) <= int(params["MIN_BARS"]) or len(history) < sma_bars + 2:
            return None

        eastern = history.index.tz_convert("America/New_York")
        minute = np.asarray(eastern.hour) * 60 + np.asarray(eastern.minute)
        same_day = np.asarray(eastern.normalize() == eastern.normalize()[-1])

        if not (same_day[-1] and rth_open <= minute[-1] < rth_close):
            return None
        if minute[-1] < rth_open + range_minutes:
            return None

        in_range = same_day & (minute >= rth_open) & (minute < rth_open + range_minutes)
        if not in_range.any():
            return None

        high = history["high"].to_numpy()
        low = history["low"].to_numpy()
        close = history["close"].to_numpy()
        volume = history["volume"].to_numpy()

        range_high = float(np.max(high[in_range]))
        range_low = float(np.min(low[in_range]))
        volume_average = float(np.mean(volume[-sma_bars:]))
        if volume_average <= 0.0:
            return None

        last_close = float(close[-1])
        prev_close = float(close[-2])
        volume_ok = volume[-1] > volume_average * volume_mult

        if last_close > range_high and prev_close <= range_high and volume_ok:
            return Decision(action="BUY", probability=0.60)
        if last_close < range_low and prev_close >= range_low:
            return Decision(action="SELL", probability=0.60)
        return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/authored/test_orb_authored.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/authored/fixtures/__init__.py tests/authored/fixtures/orb_authored.py \
        tests/authored/test_orb_authored.py
git commit -m "feat: add orb authored-strategy port fixture"
```

---

### Task 3: The streamed-evaluation benchmark

**Files:**
- Create: `scripts/sp3_benchmark.py`
- Create: `tests/authored/test_sp3_benchmark.py`

**Interfaces:**
- Consumes: `OpeningRangeBreakout` (Task 2), `AuthoredStrategy` (Task 1).
- Produces: `synthetic_session(*, conid_seed, sessions, bars_per_session=390) -> pd.DataFrame`; `stream_evaluation(module_name, class_name, frames, *, lookback, params) -> tuple[BenchmarkTiming, list]`; `BenchmarkTiming` (frozen dataclass: `bars: int`, `wall_seconds: float`; `per_bar_ms` is a property); `in_process_baseline(frames, conid=4391) -> tuple[BenchmarkTiming, int]` (times today's `Backtester` on the catalogue ORB over the same bars — the spec's "versus today's in-process backtest"); `extrapolate(*, per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points, tunables, bars_per_session, folds) -> dict` (keys include `pool_passes_per_point`, `total_bar_sends`, `hours`, `over_four_hours`, `proposed_period_sessions`); `ruling_text(estimate: dict) -> str`; `main(argv=None) -> int`.

The streamed path forks one child (via `multiprocessing` with a Pipe), sends each bar as a length-prefixed JSON frame shaped like the spec's protocol as amended (`{"v": 1, "session": s, "conid": c, "t": iso, "open":..,"high":..,"low":..,"close":..,"volume":..}`; a research session holds one conid, decision D1), and the child keeps a preallocated ring buffer trimmed to `lookback`, builds the `history` DataFrame, runs `on_bar`, and replies `{"session": s, "t": iso, "action": a, "probability": p}`. The benchmark also times today's in-process backtest of the catalogue ORB on the same synthetic bars, as the comparison baseline (spec 10 "versus today's in-process backtest").

Count the send volume from the real SP2c structure (`trader/research/cohort_evaluation.py`, `trader/research/evaluation.py`): per cohort point, `_run_point` runs `len(COST_MULTIPLIERS)=3` × `folds` passes over the fold test windows (together the pre-holdout pool minus the development segment); each neighbour (2 per numeric tunable, `trader/research/neighbours.py`) is one more pass at multiplier 1.0; the holdout is `holdout_sessions` × 2 replays. `extrapolate` reports the breakdown so the owner sees where the time goes.

- [ ] **Step 1: Write the failing test** (`tests/authored/test_sp3_benchmark.py`)

```python
"""SP3 Plan 1 Task 3: the streamed-evaluation benchmark (spec 10)."""
from __future__ import annotations

from scripts.sp3_benchmark import (
    BenchmarkTiming, extrapolate, ruling_text, stream_evaluation, synthetic_session,
)

PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}


def test_synthetic_session_is_regular_hours_utc():
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=60)
    assert len(frame) == 60
    assert str(frame.index.tz) == "UTC"
    eastern = frame.index.tz_convert("America/New_York")
    assert eastern[0].hour == 9 and eastern[0].minute == 30


def test_stream_evaluation_returns_decisions_and_timing():
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=120)
    timing, decisions = stream_evaluation(
        "tests.authored.fixtures.orb_authored", "OpeningRangeBreakout",
        [frame], lookback=420, params=PARAMS)
    assert isinstance(timing, BenchmarkTiming)
    # One bar is a warm-up, not timed, so bars is len(frame) - 1.
    assert timing.bars == len(frame) - 1 and timing.per_bar_ms > 0
    # The synthetic session is built to fire at least one breakout, so the
    # benchmark is timing real work, not an all-None pass.
    assert any(d is not None for d in decisions)


def test_extrapolate_breaks_down_the_send_volume():
    estimate = extrapolate(per_bar_ms=0.2, conids=10, period_sessions=690,
                           holdout_sessions=90, cohort_points=3, tunables=2,
                           bars_per_session=390, folds=6)
    assert estimate["total_bar_sends"] > 0
    assert estimate["hours"] > 0
    # 3 cost multipliers + 2 tunables x 2 neighbours = 7 pool passes per point.
    assert estimate["pool_passes_per_point"] == 7


def test_ruling_text_names_the_four_hour_rule():
    text = ruling_text(extrapolate(per_bar_ms=0.2, conids=10, period_sessions=690,
                                   holdout_sessions=90, cohort_points=3, tunables=2,
                                   bars_per_session=390, folds=6))
    assert "4 h" in text and "period_sessions" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest tests/authored/test_sp3_benchmark.py -q`
Expected: FAIL (`ModuleNotFoundError: scripts.sp3_benchmark`). `scripts` has no `__init__.py` but is importable through `pyproject.toml` `pythonpath` and the precedent `tests/test_parity_compare.py` (`from scripts.parity_compare import ...`); do not add `scripts/__init__.py`.

- [ ] **Step 3: Write `scripts/sp3_benchmark.py`**

```python
#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib, json, sys, time
from dataclasses import dataclass
from multiprocessing import Pipe, Process
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COST_MULTIPLIERS = 3
NEIGHBOURS_PER_TUNABLE = 2
HOLDOUT_REPLAYS = 2
FOUR_HOURS_SECONDS = 4 * 3600
MIN_PERIOD_SESSIONS = 132
PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}
COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class BenchmarkTiming:
    bars: int
    wall_seconds: float

    @property
    def per_bar_ms(self) -> float:
        return 1000.0 * self.wall_seconds / self.bars if self.bars else 0.0


def synthetic_session(*, conid_seed: int, sessions: int, bars_per_session: int = 390) -> pd.DataFrame:
    rng = np.random.default_rng(conid_seed)
    frames = []
    for day in range(sessions):
        start = pd.Timestamp("2024-01-02 14:30:00", tz="UTC") + pd.Timedelta(days=day)
        index = pd.date_range(start, periods=bars_per_session, freq="1min", tz="UTC")
        close = np.full(bars_per_session, 100.0)
        up = bars_per_session // 2
        close[up:] = 100.0 + 2.0 + rng.normal(0, 0.05, bars_per_session - up)
        volume = np.full(bars_per_session, 1000.0)
        volume[up] = 12_000.0
        frame = pd.DataFrame(
            {"open": close - 0.05, "high": close + 0.3, "low": close - 0.3,
             "close": close, "volume": volume}, index=index)
        frame.index.name = "date"
        frames.append(frame)
    combined = pd.concat(frames)
    combined.index.name = "date"
    return combined


def _child(conn, module_name, class_name, lookback, params):
    strategy = getattr(importlib.import_module(module_name), class_name)()
    buffer = np.zeros((lookback, 5), dtype=float)
    stamps: list[pd.Timestamp] = []
    count = 0
    while True:
        raw = conn.recv_bytes()
        message = json.loads(raw.decode())
        if message.get("type") == "close":
            return
        row = [message[c] for c in COLUMNS]
        if count < lookback:
            buffer[count] = row
            stamps.append(pd.Timestamp(message["t"]))
        else:
            buffer[:-1] = buffer[1:]
            buffer[-1] = row
            stamps.pop(0)
            stamps.append(pd.Timestamp(message["t"]))
        count += 1
        size = min(count, lookback)
        history = pd.DataFrame(buffer[:size], columns=COLUMNS)
        history.index = pd.DatetimeIndex(stamps, tz="UTC", name="date")
        decision = strategy.on_bar(history, params)
        conn.send_bytes(json.dumps(
            {"action": None if decision is None else decision.action}).encode())


def stream_evaluation(module_name, class_name, frames, *, lookback, params):
    parent, child = Pipe()
    process = Process(target=_child, args=(child, module_name, class_name, lookback, params))
    process.start()
    decisions: list[str | None] = []
    rows = []
    for session_index, frame in enumerate(frames):
        values = frame[list(COLUMNS)].to_numpy(dtype=float)
        times = [t.isoformat() for t in frame.index]
        for i in range(len(frame)):
            rows.append((session_index, times[i], values[i]))
    # one warm-up bar, not timed, so the child's pandas import is not charged to bar 1
    s0, t0, v0 = rows[0]
    parent.send_bytes(json.dumps({"v": 1, "session": s0, "conid": 1, "t": t0, "open": v0[0], "high": v0[1],
                                  "low": v0[2], "close": v0[3], "volume": v0[4]}).encode())
    parent.recv_bytes()
    bars = 0
    started = time.perf_counter()
    for session_index, iso, v in rows[1:]:
        parent.send_bytes(json.dumps({"v": 1, "session": session_index, "conid": 1, "t": iso, "open": v[0],
                                      "high": v[1], "low": v[2], "close": v[3], "volume": v[4]}).encode())
        decisions.append(json.loads(parent.recv_bytes().decode())["action"])
        bars += 1
    elapsed = time.perf_counter() - started
    parent.send_bytes(json.dumps({"type": "close"}).encode())
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
    return BenchmarkTiming(bars=bars, wall_seconds=elapsed), decisions


def in_process_baseline(frames, conid=4391):
    from trader.data.data_access import TickStorage
    from trader.data.duckdb_store import DuckDBDataStore
    from trader.objects import BarSize
    from trader.simulation.backtester import Backtester, BacktestConfig
    import datetime as dt, tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "bench.duckdb")
    combined = pd.concat(frames); combined.index.name = "date"
    DuckDBDataStore(path).write(str(conid), combined)
    config = BacktestConfig(start_date=combined.index[0].to_pydatetime(),
                            end_date=combined.index[-1].to_pydatetime(), bar_size=BarSize.Mins1)
    started = time.perf_counter()
    result = Backtester(TickStorage(path), config).run_from_module(
        "strategies/opening_range_breakout.py", "OpeningRangeBreakout", [conid])
    elapsed = time.perf_counter() - started
    return BenchmarkTiming(bars=len(combined), wall_seconds=elapsed), result.total_trades


def extrapolate(*, per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                tunables, bars_per_session, folds):
    pool_sessions = period_sessions - holdout_sessions
    fold_test_sessions = pool_sessions - pool_sessions // (folds + 1)
    pool_passes_per_point = COST_MULTIPLIERS + NEIGHBOURS_PER_TUNABLE * tunables
    point_sessions = pool_passes_per_point * fold_test_sessions
    holdout_pass_sessions = HOLDOUT_REPLAYS * holdout_sessions
    sessions_per_conid = cohort_points * point_sessions + holdout_pass_sessions
    total_bar_sends = conids * sessions_per_conid * bars_per_session
    seconds = total_bar_sends * per_bar_ms / 1000.0
    return {"per_bar_ms": per_bar_ms, "pool_passes_per_point": pool_passes_per_point,
            "fold_test_sessions": fold_test_sessions, "sessions_per_conid": sessions_per_conid,
            "total_bar_sends": total_bar_sends, "seconds": seconds, "hours": seconds / 3600.0,
            "over_four_hours": seconds > FOUR_HOURS_SECONDS,
            "proposed_period_sessions": _fit_period(per_bar_ms, conids, holdout_sessions,
                                                    cohort_points, tunables, bars_per_session, folds)}


def _fit_period(per_bar_ms, conids, holdout_sessions, cohort_points, tunables, bars_per_session, folds):
    for period in range(690, MIN_PERIOD_SESSIONS - 1, -1):
        e = extrapolate_seconds(per_bar_ms, conids, period, holdout_sessions, cohort_points,
                                tunables, bars_per_session, folds)
        if e <= FOUR_HOURS_SECONDS:
            return period
    return MIN_PERIOD_SESSIONS


def extrapolate_seconds(per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                        tunables, bars_per_session, folds):
    pool = period_sessions - holdout_sessions
    fold_test = pool - pool // (folds + 1)
    passes = COST_MULTIPLIERS + NEIGHBOURS_PER_TUNABLE * tunables
    per_conid = cohort_points * passes * fold_test + HOLDOUT_REPLAYS * holdout_sessions
    return conids * per_conid * bars_per_session * per_bar_ms / 1000.0


def ruling_text(estimate):
    hours = estimate["hours"]
    n = estimate["proposed_period_sessions"]
    if estimate["over_four_hours"]:
        decision = (f"reduce research_service.period_sessions for authored keys only to N={n} "
                    f"(>= {MIN_PERIOD_SESSIONS}: 6 folds, embargo 5, holdout 90); never weaken the sandbox")
    else:
        decision = (f"keep the SP2c research_service.period_sessions default (690, under 4 h); "
                    f"never weaken the sandbox")
    return (f"Plan 1 ruling 1: a full authored evaluation (10 conids x 690 sessions x 3 cohort "
            f"points, 2 tunables, 4 neighbours each, + 90-session holdout) is estimated at "
            f"{hours:.2f} h at an upper-bound combined cost of {estimate['per_bar_ms']:.4f} ms/bar "
            f"(streamed + in-process backtest, strategy time counted twice). "
            f"Since {hours:.2f} h is {'over' if estimate['over_four_hours'] else 'under'} 4 h: "
            f"{decision}.")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--conids", type=int, default=1)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--bars-per-session", type=int, default=390)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    frames = [synthetic_session(conid_seed=c, sessions=args.sessions,
                                bars_per_session=args.bars_per_session) for c in range(args.conids)]
    samples, decisions = [], []
    for _ in range(args.runs):
        timing, decisions = stream_evaluation("tests.authored.fixtures.orb_authored",
                                              "OpeningRangeBreakout", frames, lookback=420, params=PARAMS)
        samples.append(timing.per_bar_ms)
    per_bar_ms = sorted(samples)[len(samples) // 2]
    fired = sum(1 for d in decisions if d is not None)
    base, trades = in_process_baseline([synthetic_session(conid_seed=0, sessions=args.sessions,
                                                          bars_per_session=args.bars_per_session)])
    combined = per_bar_ms + base.per_bar_ms
    estimate = extrapolate(per_bar_ms=combined, conids=10, period_sessions=690, holdout_sessions=90,
                           cohort_points=3, tunables=2, bars_per_session=390, folds=6)
    print(f"streamed per-bar (median of {args.runs}): {per_bar_ms:.4f} ms ({fired} signals fired)")
    print(f"in-process backtest per-bar: {base.per_bar_ms:.4f} ms ({trades} trades)")
    print(f"upper-bound combined cost per bar (streamed + in-process backtest, "
          f"strategy time counted twice): {combined:.4f} ms")
    print(f"total bar sends (full scale): {estimate['total_bar_sends']:,}")
    print(f"estimated full evaluation: {estimate['hours']:.2f} h")
    print(ruling_text(estimate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the benchmark test to verify it passes**

Run: `uv run --frozen pytest tests/authored/test_sp3_benchmark.py -q`
Expected: PASS. (The test runs at 1 conid x 1 session, well under the 30 s per-test timeout. The child join is deterministic at this size.)

- [ ] **Step 5: Commit**

```bash
git add scripts/sp3_benchmark.py tests/authored/test_sp3_benchmark.py
git commit -m "feat: add streamed-evaluation benchmark for authored strategies"
```

---

### Task 4: The allow-lists and the shipped-library walk

**Files:**
- Create: `trader/authored/allowed_api.py`
- Create: `tests/authored/test_allowed_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces (Task 5 and Plan 2's in-sandbox checker use these exactly):
  - `ALLOWED_IMPORTS: frozenset[str]` — `{"numpy", "pandas", "math", "statistics", "trader.authored.api"}`. `typing` and `dataclasses` are from-import only (ruling 24); every entry except `trader.authored.api` (which a dotted `import` never reaches) has an `ALLOWED_MODULE_ATTRIBUTES` list.
  - `ALLOWED_MODULE_ATTRIBUTES: Mapping[str, frozenset[str]]` — per module (`numpy`, `pandas`, `math`, `statistics`) the attributes an authored file may read off that module.
  - `DENIED_CHAIN_NAMES: frozenset[str]` — names refused anywhere in a chain.
  - `ALLOWED_FROM_IMPORTS: frozenset[str]` — modules an authored file may `from X import name` (`typing`, `dataclasses`, `trader.authored.api`).

Before writing the allow-list, confirm every name resolves on the shipped pandas 3.0.6 / numpy 2.5.3 and that none is a module (numpy 2 removed `NaN`, `float_`, `product`; do not list them). The walk test below enforces this, so run it after writing.

- [ ] **Step 1: Write the failing test** (`tests/authored/test_allowed_api.py`)

```python
"""SP3 Plan 1 Task 4: the allow-lists walk the shipped pandas/numpy (spec 7.2)."""
from __future__ import annotations

import importlib
import inspect

import pytest

from trader.authored.allowed_api import (
    ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
)

PROCESS_MODULES = {"os", "sys", "subprocess", "builtins"}


def test_imports_are_the_spec_seven_modules():
    assert ALLOWED_IMPORTS == frozenset(
        {"numpy", "pandas", "math", "statistics", "dataclasses", "typing",
         "trader.authored.api"})


def test_from_imports_exclude_the_data_modules():
    assert "numpy" not in ALLOWED_FROM_IMPORTS and "pandas" not in ALLOWED_FROM_IMPORTS
    assert ALLOWED_FROM_IMPORTS == frozenset({"typing", "dataclasses", "trader.authored.api"})


def test_deny_list_covers_the_spec_and_the_rulings():
    for name in ("os", "sys", "io", "subprocess", "signal", "kill", "getppid",
                 "system", "popen", "modules", "builtins", "ctypeslib", "pickle",
                 "ctypes", "dump", "dumps", "loads", "frombuffer"):
        assert name in DENIED_CHAIN_NAMES


@pytest.mark.parametrize("module_name", sorted(ALLOWED_MODULE_ATTRIBUTES))
def test_every_allowed_attribute_is_not_a_module_and_hides_no_process_module(module_name):
    module = importlib.import_module(module_name)
    for attribute in sorted(ALLOWED_MODULE_ATTRIBUTES[module_name]):
        assert hasattr(module, attribute), f"{module_name}.{attribute} is gone on this build"
        value = getattr(module, attribute)
        assert not inspect.ismodule(value), f"{module_name}.{attribute} is a module"
        for public in dir(value):
            if public.startswith("_"):
                continue
            try:
                nested = getattr(value, public)
            except Exception:
                continue
            assert not (inspect.ismodule(nested) and nested.__name__ in PROCESS_MODULES), (
                f"{module_name}.{attribute}.{public} exposes {PROCESS_MODULES}")


def test_statistics_submodules_are_never_listed():
    # statistics imports math, numbers, random, sys as attributes; none may be allowed.
    for submodule in ("random", "sys", "math", "numbers"):
        assert submodule not in ALLOWED_MODULE_ATTRIBUTES["statistics"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest tests/authored/test_allowed_api.py -q`
Expected: FAIL (`ModuleNotFoundError: trader.authored.allowed_api`).

- [ ] **Step 3: Write `trader/authored/allowed_api.py`**

```python
"""The static checker's allow-lists and deny-list, as data (spec 7.2).

An authored file may import only ALLOWED_IMPORTS. An attribute read off an
imported data module (np.x, pd.x, math.x, statistics.x) must be in
ALLOWED_MODULE_ATTRIBUTES[module]; nothing listed is a module (the walk test
in tests/authored/test_allowed_api.py proves it on the shipped build).
DENIED_CHAIN_NAMES are refused anywhere in an attribute chain. The lists
extend the spec's (Plan 1 ruling 6); they never narrow it.
"""
from __future__ import annotations

from typing import Mapping

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"numpy", "pandas", "math", "statistics", "dataclasses", "typing",
     "trader.authored.api"})

ALLOWED_FROM_IMPORTS: frozenset[str] = frozenset(
    {"typing", "dataclasses", "trader.authored.api"})

ALLOWED_MODULE_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "numpy": frozenset({
        "abs", "absolute", "arange", "argmax", "argmin", "argsort", "array",
        "asarray", "bool_", "ceil", "clip", "concatenate", "corrcoef", "cov",
        "cumprod", "cumsum", "diff", "exp", "float64", "floor", "full", "inf",
        "int64", "isfinite", "isnan", "log", "log1p", "max", "maximum", "mean",
        "median", "min", "minimum", "nan", "nanmax", "nanmean", "nanmedian", "nanmin",
        "nanstd", "nansum", "ones", "percentile", "quantile", "sign", "sort",
        "sqrt", "std", "sum", "unique", "var", "where", "zeros", "round"}),
    "pandas": frozenset({
        "DataFrame", "Series", "Timedelta", "Timestamp", "DatetimeIndex",
        "Index", "isna", "notna", "concat", "to_datetime", "date_range"}),
    "math": frozenset({
        "ceil", "copysign", "e", "exp", "fabs", "floor", "fsum", "inf",
        "isfinite", "isinf", "isnan", "log", "log10", "nan", "pi", "sqrt",
        "tanh", "erf"}),
    "statistics": frozenset({
        "mean", "median", "median_low", "median_high", "stdev", "pstdev",
        "variance", "pvariance", "fmean", "quantiles"}),
}

DENIED_CHAIN_NAMES: frozenset[str] = frozenset({
    # spec 7.2 deny-list of module and process names
    "os", "sys", "io", "common", "core", "compat", "api", "lib", "util",
    "testing", "builtins", "modules", "subprocess", "signal", "kill",
    "getppid", "system", "popen", "plot", "style", "sql",
    # Plan 1 ruling 6 additions
    "ctypeslib", "pickle", "ctypes", "dump", "dumps", "loads", "frombuffer"})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --frozen pytest tests/authored/test_allowed_api.py -q`
Expected: PASS. If any `MISSING`/`is a module` assertion fires, remove that name from the allow-list (it does not resolve or is a submodule on this build) and re-run.

- [ ] **Step 5: Commit**

```bash
git add trader/authored/allowed_api.py tests/authored/test_allowed_api.py
git commit -m "feat: add authored-strategy allow-lists with shipped-library walk"
```

---

### Task 5: The static checker

**Files:**
- Create: `trader/authored/static_check.py`
- Create: `tests/authored/test_static_check.py`

**Interfaces:**
- Consumes: `ALLOWED_IMPORTS`, `ALLOWED_FROM_IMPORTS`, `ALLOWED_MODULE_ATTRIBUTES`, `DENIED_CHAIN_NAMES` (Task 4); `AuthoredStrategy` name is matched by string, not imported.
- Produces (Plans 2 and 3 use these exactly):
  - `StaticViolation(rule: str, line: int, detail: str)` — frozen dataclass.
  - `check_source(source: str) -> list[StaticViolation]` — pure, no execution; `[]` means clean.
  - `AuthoredStaticRejected(Exception)` — attribute `violations: tuple[StaticViolation, ...]`.
  - `require_clean(source: str) -> str` — returns the single `AuthoredStrategy` subclass name, or raises `AuthoredStaticRejected`.
  - `MAX_SOURCE_BYTES = 20_480`.

Rule names (the `rule` field, each maps to `AUTHORED_STATIC_REJECTED` at the caller): `oversize`, `unparseable`, `import_not_allowed`, `import_star`, `from_import_not_allowed`, `dunder_name`, `banned_name`, `module_alias_misuse`, `module_attribute_not_allowed`, `io_attribute`, `string_literal`, `module_statement`, `class_shape`, `banned_statement`.

- [ ] **Step 1: Write the failing tests** (`tests/authored/test_static_check.py`)

```python
"""SP3 Plan 1 Task 5: the static checker (spec 7.2)."""
from __future__ import annotations

import pytest

from trader.authored.static_check import (
    AuthoredStaticRejected, check_source, require_clean,
)

CLEAN = '''
import numpy as np
import pandas as pd
from typing import Mapping

from trader.authored.api import AuthoredStrategy, Decision


class Good(AuthoredStrategy):
    LOOKBACK_BARS = 50
    THRESHOLD = 1.5

    def on_bar(self, history, params):
        close = history["close"].to_numpy()
        if np.mean(close[-5:]) > close[-1] * params["THRESHOLD"]:
            return Decision(action="BUY", probability=0.6)
        return None
'''


def _rules(source):
    return {v.rule for v in check_source(source)}


def test_a_clean_file_passes_and_require_clean_returns_the_class():
    assert check_source(CLEAN) == []
    assert require_clean(CLEAN) == "Good"


def test_an_import_outside_the_allow_list_is_refused():
    assert "import_not_allowed" in _rules("import os\n" + CLEAN)


def test_a_from_import_of_a_data_module_is_refused():
    assert "from_import_not_allowed" in _rules("from numpy import mean\n" + CLEAN)


def test_import_star_is_refused():
    assert "import_star" in _rules("from numpy import *\n" + CLEAN)


def test_a_submodule_chain_is_refused():
    body = CLEAN.replace("np.mean(close[-5:])", "pd.io.common.os")
    assert "module_attribute_not_allowed" in _rules(body) or "banned_name" in _rules(body)


def test_a_module_alias_used_bare_is_refused():
    body = CLEAN.replace("return None", "x = np\n        return None")
    assert "module_alias_misuse" in _rules(body)


def test_a_dunder_name_is_refused():
    body = CLEAN.replace("close[-1]", "close.__class__")
    assert "dunder_name" in _rules(body)


def test_a_banned_builtin_is_refused():
    body = CLEAN.replace("np.mean(close[-5:])", "eval('1')")
    assert "banned_name" in _rules(body)


def test_an_io_attribute_is_refused():
    body = CLEAN.replace("history[\"close\"].to_numpy()", "history.to_pickle('p')")
    assert "io_attribute" in _rules(body)


def test_to_numpy_is_allowed():
    assert "io_attribute" not in _rules(CLEAN)


def test_a_dunder_in_a_string_is_refused():
    body = CLEAN.replace("probability=0.6", "probability=0.6, ) if '{0.__globals__}' else Decision(action='BUY', probability=0.6")
    assert "string_literal" in _rules(body)


def test_a_decorator_is_refused():
    body = CLEAN.replace("    def on_bar", "    @staticmethod\n    def on_bar")
    assert "banned_statement" in _rules(body)


def test_try_except_is_refused():
    body = CLEAN.replace("        return None",
                         "        try:\n            return None\n        except Exception:\n            return None")
    assert "banned_statement" in _rules(body)


def test_two_classes_are_refused():
    body = CLEAN + "\n\nclass Other(AuthoredStrategy):\n    def on_bar(self, history, params):\n        return None\n"
    assert "class_shape" in _rules(body)


def test_a_module_level_statement_is_refused():
    assert "module_statement" in _rules(CLEAN + "\nprint('hi')\n")


def test_a_file_that_does_not_parse_is_one_violation():
    violations = check_source("class Good(AuthoredStrategy:\n")
    assert len(violations) == 1 and violations[0].rule == "unparseable"


def test_an_oversize_file_is_refused():
    assert "oversize" in _rules(CLEAN + "\n# " + "x" * 20_500)


def test_require_clean_raises_with_violations():
    with pytest.raises(AuthoredStaticRejected) as caught:
        require_clean("import os\n" + CLEAN)
    assert caught.value.violations
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/authored/test_static_check.py -q`
Expected: FAIL (`ModuleNotFoundError: trader.authored.static_check`).

- [ ] **Step 3: Write `trader/authored/static_check.py`**

```python
"""Static checker for authored strategies (spec 7.2). Pure AST walk, no
execution. The second layer of SP3's defence; Plan 2's process boundary is the
isolation. Every failure is one StaticViolation whose rule names the broken
rule; the caller maps it to AUTHORED_STATIC_REJECTED.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from trader.authored.allowed_api import (
    ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
)

MAX_SOURCE_BYTES = 20_480
BASE_CLASS = "AuthoredStrategy"
_DATA_MODULES = frozenset(ALLOWED_MODULE_ATTRIBUTES)

BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "input", "breakpoint",
    "memoryview", "format"})
BANNED_ATTRIBUTES = frozenset({"format", "format_map"})
_IO_PREFIX = re.compile(r"^(read_|to_)")
_IO_NAMES = frozenset({"load", "save", "savez", "fromfile", "tofile", "eval",
                       "query", "ctypeslib", "pickle"})


@dataclass(frozen=True)
class StaticViolation:
    rule: str
    line: int
    detail: str


class AuthoredStaticRejected(Exception):
    def __init__(self, violations: tuple[StaticViolation, ...]):
        self.violations = violations
        super().__init__(f"{len(violations)} static violation(s)")


def _is_io_attribute(name: str) -> bool:
    if name == "to_numpy":
        return False
    return bool(_IO_PREFIX.match(name)) or name in _IO_NAMES


def _bad_string(text: str) -> bool:
    if "__" in text:
        return True
    if text in DENIED_CHAIN_NAMES or text in BANNED_NAMES:
        return True
    return _is_io_attribute(text)


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[StaticViolation] = []
        self.module_aliases: dict[str, str] = {}   # alias -> real module name

    def add(self, rule: str, node: ast.AST, detail: str) -> None:
        self.violations.append(StaticViolation(rule, getattr(node, "lineno", 0), detail))

    # --- imports -------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in ALLOWED_IMPORTS or "." in alias.name:
                self.add("import_not_allowed", node, f"import {alias.name}")
                continue
            if alias.name in _DATA_MODULES:
                self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if any(a.name == "*" for a in node.names):
            self.add("import_star", node, f"from {module} import *")
        elif module not in ALLOWED_FROM_IMPORTS:
            self.add("from_import_not_allowed", node, f"from {module} import ...")
        self.generic_visit(node)

    # --- names, attributes, strings -----------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        self._check_identifier(node, node.id)
        if node.id in self.module_aliases and not self._is_chain_root(node):
            self.add("module_alias_misuse", node, f"module alias {node.id} used outside a chain root")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_identifier(node, node.attr)
        if node.attr in BANNED_ATTRIBUTES:
            self.add("banned_name", node, f"attribute {node.attr}")
        if _is_io_attribute(node.attr):
            self.add("io_attribute", node, f"attribute {node.attr}")
        root = self._chain_root(node)
        if isinstance(root, ast.Name) and root.id in self.module_aliases:
            self._check_module_chain(node, root.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _bad_string(node.value):
            self.add("string_literal", node, f"string literal {node.value!r}")
        self.generic_visit(node)

    def _check_identifier(self, node: ast.AST, name: str) -> None:
        if name.startswith("_"):
            self.add("dunder_name", node, f"name {name}")
        elif name in BANNED_NAMES:
            self.add("banned_name", node, f"name {name}")
        elif name in DENIED_CHAIN_NAMES:
            self.add("banned_name", node, f"denied chain name {name}")

    def _check_module_chain(self, node: ast.Attribute, alias: str) -> None:
        # The attribute directly on the alias is the "first attribute".
        if isinstance(node.value, ast.Name) and node.value.id == alias:
            module = self.module_aliases[alias]
            if node.attr not in ALLOWED_MODULE_ATTRIBUTES[module]:
                self.add("module_attribute_not_allowed", node, f"{alias}.{node.attr}")

    @staticmethod
    def _chain_root(node: ast.AST) -> ast.AST:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node

    @staticmethod
    def _is_chain_root(name_node: ast.Name) -> bool:
        parent = getattr(name_node, "_parent", None)
        return isinstance(parent, ast.Attribute) and parent.value is name_node

    # --- banned statements --------------------------------------------
    def visit_Global(self, node: ast.Global) -> None:
        self.add("banned_statement", node, "global")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.add("banned_statement", node, "nonlocal")

    def visit_Try(self, node: ast.Try) -> None:
        self.add("banned_statement", node, "try/except")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.add("banned_statement", node, "async def")

    def visit_Await(self, node: ast.Await) -> None:
        self.add("banned_statement", node, "await")

    def visit_Yield(self, node: ast.Yield) -> None:
        self.add("banned_statement", node, "yield")

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.add("banned_statement", node, "yield from")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # kw_defaults is a list with a None per keyword-only arg that has no default,
        # so test for a real default, not just a non-empty list.
        if node.args.defaults or any(d is not None for d in node.args.kw_defaults):
            self.add("banned_statement", node, "lambda with defaults")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.decorator_list:
            self.add("banned_statement", node, f"decorator on {node.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.decorator_list:
            self.add("banned_statement", node, f"decorator on {node.name}")
        self.generic_visit(node)


def _link_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]


def _check_module_shape(tree: ast.Module, checker: _Checker) -> None:
    classes = 0
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # a module docstring is allowed
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.ClassDef):
            classes += 1
            bases = {b.id for b in statement.bases if isinstance(b, ast.Name)}
            if BASE_CLASS not in bases:
                checker.add("class_shape", statement,
                            f"class {statement.name} does not subclass {BASE_CLASS}")
            continue
        checker.add("module_statement", statement,
                    f"module-level {type(statement).__name__}")
    if classes != 1:
        checker.add("class_shape", tree, f"expected exactly one class, found {classes}")


def check_source(source: str) -> list[StaticViolation]:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return [StaticViolation("oversize", 0, f"over {MAX_SOURCE_BYTES} bytes")]
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [StaticViolation("unparseable", error.lineno or 0, str(error.msg))]
    _link_parents(tree)
    checker = _Checker()
    _check_module_shape(tree, checker)
    checker.visit(tree)
    return sorted(checker.violations, key=lambda v: (v.line, v.rule, v.detail))


def require_clean(source: str) -> str:
    violations = check_source(source)
    if violations:
        raise AuthoredStaticRejected(tuple(violations))
    tree = ast.parse(source)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            return statement.name
    raise AuthoredStaticRejected(
        (StaticViolation("class_shape", 0, f"no {BASE_CLASS} subclass"),))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/authored/test_static_check.py -q`
Expected: PASS. If `test_a_dunder_name_is_refused` surfaces `banned_name` (because `__class__` also matches an attribute path) the assertion checks `dunder_name` is in the set, which it is; both may appear.

- [ ] **Step 5: Commit**

```bash
git add trader/authored/static_check.py tests/authored/test_static_check.py
git commit -m "feat: add authored-strategy static checker"
```

---

### Task 6: The escape corpus and the ORB-passes-the-checker test

**Files:**
- Create: `tests/authored/escapes/` with the files below (data, not collected by pytest)
- Create: `tests/authored/test_escape_corpus.py`

**Interfaces:**
- Consumes: `check_source`, `require_clean` (Task 5); `OpeningRangeBreakout` fixture path (Task 2).
- Produces: `tests/authored/escapes/*.py` — the shared corpus Plan 2 reuses to prove each file also fails *inside* the sandbox (that is Plan 2's test; this plan proves only static rejection). Every corpus file covers a case from spec section 10 plus rulings 6–8. `SANDBOX_HOSTILE` in `tests/authored/test_escape_corpus.py`: the nine file stems Plan 2's `HOSTILE_ESCAPES` names, exactly (SP3 decision D9).

- [ ] **Step 1: Write the corpus files** under `tests/authored/escapes/`. Each is a plausible authored file that must be rejected. Do not add `__init__.py` to this directory.

`escape_subclasses_walk.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        klass = ().__class__.__bases__[0]
        for sub in klass.__subclasses__():
            if sub.__name__ == "Popen":
                sub(["/bin/true"])
        return Decision(action="BUY", probability=0.5)
```

`escape_format_globals.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        leak = "{0.__globals__[__builtins__]}".format(self)
        return Decision(action="BUY", probability=0.5) if leak else None
```

`escape_pd_eval.py`:
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        pd.eval("1 + 1")
        return Decision(action="BUY", probability=0.5)
```

`escape_df_query.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.query("close > 0")
        return Decision(action="BUY", probability=0.5)
```

`escape_pickle.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        blob = history["close"].to_numpy().dumps()
        return Decision(action="BUY", probability=0.5) if blob else None
```

`escape_importlib_string.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        name = "importlib"
        module = __import__(name)
        return Decision(action="BUY", probability=0.5) if module else None
```

`escape_computed_getattr.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        call = getattr(history, "to_pickle")
        call("/tmp/x")
        return Decision(action="BUY", probability=0.5)
```

`escape_decorator.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    @property
    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.5)
```

`escape_lambda_default.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        sneak = lambda builtins=__builtins__: builtins
        return Decision(action="BUY", probability=0.5) if sneak() else None
```

`escape_try_except.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        try:
            return Decision(action="BUY", probability=0.5)
        except BaseException:
            return None
```

`escape_pd_io_common_oskill.py` (the exact OpenAI path, spec 10):
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        pd.io.common.os.kill(pd.io.common.os.getppid(), 9)
        return Decision(action="BUY", probability=0.5)
```

`escape_import_os.py`:
```python
import os

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        os.system("true")
        return Decision(action="BUY", probability=0.5)
```

`escape_alias_rebind.py` (Plan 1 ruling 7):
```python
import numpy as np

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        smuggle = np
        return Decision(action="BUY", probability=0.5) if smuggle.ctypeslib else None
```

`escape_string_dispatch.py` (Plan 1 ruling 8):
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.agg("to_pickle", path="/tmp/x")
        return Decision(action="BUY", probability=0.5)
```

The next nine files are the runtime-hostile set Plan 2 loads as probes with the static check skipped (Plan 2 `HOSTILE_ESCAPES`, Plan 2 ruling 11; spec 10 "hostile session"). Each must be refused by `check_source` **and** raise at run time inside the sandbox. `escape_pd_io_common_oskill.py` above is the ninth (the exact OpenAI path). Where the hostile act itself passes the checker (a large `np.ones`, a busy loop), the file names a local with a leading underscore: that is its static violation and changes nothing at run time.

`escape_read_config_keys.py` (`/home/trader/.config` is a root-owned 0700 tmpfs, so a non-root child cannot traverse it; no keys are mounted either):
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        keys = pd.read_csv("/home/trader/.config/mmr/keys/rpc/trader.key")
        return Decision(action="BUY", probability=0.5) if len(keys) else None
```

`escape_write_outside_tmpfs.py` (writes to the read-only `/store` mount, never the writable `/tmp` tmpfs, so the write genuinely fails; review r2 #1):
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.to_csv("/store/escape.csv")       # /store is the read-only authored-strategy mount
        return Decision(action="BUY", probability=0.5)
```

`escape_fork_bomb.py`:
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        while True:
            pd.io.common.os.fork()
```

`escape_allocate_past_limit.py`:
```python
import numpy as np

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        _hog = np.ones(2 ** 33)
        return Decision(action="BUY", probability=0.5) if _hog.sum() else None
```

`escape_loop_forever.py`:
```python
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        _spin = 0
        while True:
            _spin += 1
```

`escape_kill_other_child.py` (kills every other slot-uid process; Plan 2 holds a benign session open while it runs):
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        mine = pd.io.common.os.getpid()
        for name in pd.io.common.os.listdir("/proc"):
            if name.isdigit() and int(name) != mine and pd.io.common.os.stat("/proc/" + name).st_uid >= 20001:
                pd.io.common.os.kill(int(name), 9)
        return Decision(action="BUY", probability=0.5)
```

`escape_sys_modules_lookup.py`:
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        posix = pd.io.common.os.sys.modules["posix"]
        posix.kill(posix.getppid(), 9)
        return Decision(action="BUY", probability=0.5)
```

`escape_open_socket.py`:
```python
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        net = pd.io.common.os.sys.modules["socket"]
        conn = net.create_connection(("1.1.1.1", 443), 1)
        return Decision(action="BUY", probability=0.5) if conn else None
```

**Corpus recheck (review r2 #1).** Each hostile file must be impossible in the real container at `BAR`, and Plan 2's `EXPECTED_FAULT_MARKERS` names the error that proves the attempt failed (never a load error). Checked one by one:

| File | Attempt | Why it fails in the sandbox | Expected error |
|---|---|---|---|
| `escape_read_config_keys` | read the RPC key file | `/home/trader/.config` is a root-owned 0700 tmpfs a non-root child cannot traverse (no keys mounted either) | `PermissionError` |
| `escape_write_outside_tmpfs` | write under `/store` | `/store` is a read-only mount (**changed** from `/tmp`, which is a writable tmpfs) | `Read-only file system` |
| `escape_fork_bomb` | `os.fork()` | `RLIMIT_NPROC` 1 refuses a new process | `BlockingIOError` / `Resource temporarily unavailable` |
| `escape_allocate_past_limit` | `np.ones(2**33)` (64 GB) | `RLIMIT_AS` is baseline + 256 MiB | `MemoryError` / `Unable to allocate` |
| `escape_loop_forever` | busy loop | the 2 s per-bar wall timeout kills the child | `bar timeout` |
| `escape_pd_io_common_oskill` | `os.kill(getppid(), 9)` | the child has no `CAP_KILL` and the supervisor is uid 0 | `PermissionError` (EPERM) |
| `escape_kill_other_child` | kill another slot child | each child has its own uid; cross-uid kill is EPERM | `PermissionError` (EPERM) |
| `escape_sys_modules_lookup` | `posix.kill(getppid(), 9)` via `os.sys.modules` | same EPERM | `PermissionError` (EPERM) |
| `escape_open_socket` | open a TCP connection | `network_mode: none` (no route) | `OSError` / `Network is unreachable` |

Only `escape_write_outside_tmpfs` needed a change (its old `/tmp/escape.csv` would have succeeded on the writable tmpfs). The two static-only files (`escape_subclasses_walk` and the like) are refused by `check_source` and never loaded in the sandbox; they are not in `HOSTILE_ESCAPES`.

- [ ] **Step 2: Write the failing test** (`tests/authored/test_escape_corpus.py`)

```python
"""SP3 Plan 1 Task 6: every escape file is statically rejected (spec 10).

The corpus is shared with Plan 2, which proves each file also fails inside the
sandbox. This plan proves only the static rejection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trader.authored.static_check import check_source, require_clean, AuthoredStaticRejected

ESCAPE_DIR = Path(__file__).parent / "escapes"
ESCAPES = sorted(ESCAPE_DIR.glob("*.py"))
FIXTURE = Path(__file__).parent / "fixtures" / "orb_authored.py"

# The runtime-hostile files Plan 2 loads with the static check skipped (Plan 2 HOSTILE_ESCAPES, D9).
SANDBOX_HOSTILE = (
    "escape_read_config_keys", "escape_write_outside_tmpfs", "escape_fork_bomb",
    "escape_allocate_past_limit", "escape_loop_forever", "escape_pd_io_common_oskill",
    "escape_kill_other_child", "escape_sys_modules_lookup", "escape_open_socket",
)


def test_the_corpus_is_not_empty():
    assert len(ESCAPES) >= 22


@pytest.mark.parametrize("path", ESCAPES, ids=lambda p: p.name)
def test_every_escape_file_is_rejected(path):
    violations = check_source(path.read_text())
    assert violations, f"{path.name} was not rejected"


@pytest.mark.parametrize("stem", SANDBOX_HOSTILE)
def test_every_sandbox_hostile_file_exists_and_is_rejected(stem):
    path = ESCAPE_DIR / f"{stem}.py"
    assert path.is_file(), f"Plan 2 loads {stem} as a hostile probe; ship it here"
    assert check_source(path.read_text()), f"{stem} was not rejected"


def test_the_orb_fixture_passes_the_checker():
    assert require_clean(FIXTURE.read_text()) == "OpeningRangeBreakout"
```

- [ ] **Step 3: Run the test to verify each file is rejected and the ORB fixture passes**

Run: `uv run --frozen pytest tests/authored/test_escape_corpus.py -q`
Expected: PASS. If `test_the_orb_fixture_passes_the_checker` fails, the fixture (Task 2) violates a rule the checker enforces — fix the fixture, not the checker (the checker is the contract). A likely culprit: an attribute whose name collides with the deny-list; rename the local variable.

- [ ] **Step 4: Commit**

```bash
git add tests/authored/escapes tests/authored/test_escape_corpus.py
git commit -m "test: add authored-strategy escape corpus and orb checker pass"
```

---

### Task 7: Run the benchmark, record the ruling, and run the full suite

**Files:**
- Modify: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md` (replace the estimate with the measured ruling), `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md` (section 14, only if the numbers differ)
- Modify: this plan file (fill Plan 1 ruling 1 with the measured numbers)

- [ ] **Step 1: Run the benchmark at a scaled-down size and read its output**

Run: `uv run --frozen python scripts/sp3_benchmark.py --conids 1 --sessions 40 --runs 3`
Expected: it prints the median per-bar ms, the send-volume breakdown, the full-scale hour estimate, and the `Plan 1 ruling 1:` line. Confirm the "signals fired" count is non-zero (otherwise the benchmark timed an all-`None` pass; increase `--sessions`).

- [ ] **Step 2: Fill Plan 1 ruling 1 in this plan and append it to the index**

Replace the planning estimate in this file's ruling 1 with the printed `Plan 1 ruling 1:` line (exact numbers). Then replace the "Plan 1 ruling 1" line under "## Owner rulings to confirm" → "Benchmark" in `2026-10-11-ai-paper-sp3-00-index.md` with the same line, and update the "Evaluation length" bullet of spec section 14 if the measured hours or the proposed session count differ.

- [ ] **Step 3: Commit the ruling**

```bash
git add docs/superpowers/plans/2026-10-11-ai-paper-sp3-01-api-checker-benchmark.md \
        docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md \
        docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md
git commit -m "chore: record sp3 plan 1 benchmark ruling"
```

- [ ] **Step 4: Run the full suite under the shared lock**

Run (the shared lock is a directory, not `flock`, which macOS lacks; the basetemp is outside `/private/tmp/claude-502`, per the owner's suite-run note):
```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
uv run --frozen pytest tests/ -q -n 8 --timeout=120 --timeout-method=thread \
  --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/pytest-sp3-p1; status=$?
chmod -R u+w /private/tmp/pytest-sp3-p1; rm -rf /private/tmp/pytest-sp3-p1
rmdir /private/tmp/mmr-suite.lock
exit $status
```
Expected: PASS (the new `tests/authored/` tests included). If pytest-xdist is missing, run single-process with `--timeout=60` instead of `-n 8 --timeout=120`. If a benchmark test flakes on a loaded machine (the child join), re-run `tests/authored/test_sp3_benchmark.py` alone; it is deterministic at 1 conid × 1 session.

- [ ] **Step 5: Commit any fixes from the full run** (only if needed)

```bash
git add tests/authored trader/authored scripts/sp3_benchmark.py
git commit -m "test: fix fallout from the full suite run"
```

---

## Self-Review

**1. Spec coverage.** 7.1 → Task 1 (`AuthoredStrategy`, `Decision`, `LOOKBACK_BARS`); keys from section 3 → Task 1. 7.2 allow-lists → Task 4; checker rules (imports, underscore, banned names, I/O attrs, module-attribute allow-list, chain deny-list, module-statement/class shape, banned statements) → Task 5; shipped pandas/numpy walk → Task 4. Section 10: escape corpus (every listed case plus the exact `pd.io.common.os.kill(...)`) → Task 6; static-check tests → Task 5/6; equivalence fixture (ORB port) → Task 2; benchmark → Task 3/7. Out of scope here and owned elsewhere: the store, RPC, sandbox, author loop (Plans 2–5).

**2. Placeholder scan.** No TBD/TODO; every code step is complete. The one deliberate removal (dead `_send`/`struct`) is an explicit step with a reason, not a placeholder.

**3. Type consistency.** `check_source`/`require_clean`/`StaticViolation`/`AuthoredStaticRejected` names match across Tasks 5 and 6. `ALLOWED_*`/`DENIED_CHAIN_NAMES` match across Tasks 4 and 5. `OpeningRangeBreakout` and its five tunables match across Tasks 2, 3 and 6. `source_digest`/`authored_key` names match the index.

**4. Review Focus.** All five uncovered inputs have a pinning test in the owning task (parse failure → Task 5; computed dunder → Task 6; submodule name → Task 4; `LOOKBACK_BARS` range → Task 1; bad probability → Task 1).

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-11-ai-paper-sp3-01-api-checker-benchmark.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — a fresh subagent implements each task and a fresh reviewer checks it before the next starts, then a whole-branch review at the end.
- **Native** — I implement every task myself in this session, then one fresh reviewer on the most capable model checks the whole branch.

For this plan I recommend **subagent-driven**, because the static checker and its escape corpus are security-critical (a shipped mistake lets AI code reach a real process), the tasks have clean interfaces (api → fixture → benchmark; allow-lists → checker → corpus), and an independent per-task gate is cheap insurance against a hole in the checker.
