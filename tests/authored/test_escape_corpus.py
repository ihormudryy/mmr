"""SP3 Plan 1 Task 6: every escape file is statically rejected (spec 10).

The corpus is shared with Plan 2, which proves each file also fails inside the
sandbox. This plan proves only the static rejection. Corpus files are data:
they are read as text and never imported or executed.
"""
from __future__ import annotations

from pathlib import Path

from trader.authored.static_check import check_source, require_clean
import pytest

ESCAPE_DIR = Path(__file__).parent / "escapes"
ESCAPES = sorted(ESCAPE_DIR.glob("*.py"))
FIXTURE = Path(__file__).parent / "fixtures" / "orb_authored.py"

# The runtime-hostile files Plan 2 loads with the static check skipped (Plan 2 HOSTILE_ESCAPES, D9).
SANDBOX_HOSTILE = (
    "escape_read_config_keys", "escape_write_outside_tmpfs", "escape_fork_bomb",
    "escape_allocate_past_limit", "escape_loop_forever", "escape_pd_io_common_oskill",
    "escape_kill_other_child", "escape_sys_modules_lookup", "escape_open_socket",
)

# The rule that catches each file's HOSTILE ACT, not an incidental line.
# None: the hostile act is legal Python the checker cannot judge (a resource
# attack). Those files are refused only for their deliberate marker local, named in
# RESOURCE_ATTACK_MARKER. The sandbox limits are their real test (Plan 2).
EXPECTED_RULE: dict[str, str | None] = {
    "escape_alias_rebind": "module_alias_misuse",
    "escape_allocate_past_limit": None,
    "escape_computed_getattr": "banned_builtin",
    "escape_cython_func_globals": "banned_name",
    "escape_decorator": "banned_statement",
    "escape_df_query": "io_attribute",
    "escape_fork_bomb": "module_attribute_not_allowed",
    "escape_format_globals": "banned_name",
    "escape_generator_frame": "banned_name",
    "escape_import_os": "import_not_allowed",
    "escape_importlib_string": "banned_builtin",
    "escape_kill_other_child": "module_attribute_not_allowed",
    "escape_lambda_default": "banned_statement",
    "escape_loop_forever": None,
    "escape_match_class_pattern": "match_statement",
    "escape_open_socket": "module_attribute_not_allowed",
    "escape_pd_eval": "module_attribute_not_allowed",
    "escape_pd_io_common_oskill": "module_attribute_not_allowed",
    "escape_pickle": "banned_name",
    "escape_read_config_keys": "module_attribute_not_allowed",
    "escape_string_dispatch": "banned_name",
    "escape_subclasses_walk": "dunder_name",
    "escape_sys_modules_lookup": "module_attribute_not_allowed",
    "escape_try_except": "banned_statement",
    "escape_trystar": "node_type",
    "escape_type_builtin": "banned_builtin",
    "escape_type_params": "banned_statement",
    "escape_typing_operator_chain": "import_not_allowed",
    "escape_write_outside_tmpfs": "io_attribute",
}
RESOURCE_ATTACK_MARKER = {
    "escape_allocate_past_limit": "_hog",
    "escape_loop_forever": "_spin",
}


def test_the_corpus_is_not_empty():
    assert len(ESCAPES) >= 27


def test_the_corpus_is_not_collected_or_importable_by_pytest():
    assert not (ESCAPE_DIR / "__init__.py").exists()
    assert all(not path.name.startswith("test_") and not path.name.endswith("_test.py")
               for path in ESCAPES)


@pytest.mark.parametrize("path", ESCAPES, ids=lambda p: p.name)
def test_every_escape_file_is_rejected(path):
    violations = check_source(path.read_text())
    assert violations, f"{path.name} was not rejected"


def test_every_corpus_file_has_an_expected_rule():
    assert {path.stem for path in ESCAPES} == set(EXPECTED_RULE)
    assert {stem for stem, rule in EXPECTED_RULE.items() if rule is None} == set(RESOURCE_ATTACK_MARKER)


@pytest.mark.parametrize("path", ESCAPES, ids=lambda p: p.name)
def test_the_hostile_act_is_caught_by_its_expected_rule(path):
    rule = EXPECTED_RULE[path.stem]
    violations = check_source(path.read_text())
    if rule is None:
        marker_details = {f"name {RESOURCE_ATTACK_MARKER[path.stem]}"}
        assert {v.rule for v in violations} == {"dunder_name"}
        assert {v.detail for v in violations} == marker_details
    else:
        assert rule in {v.rule for v in violations}, [(v.rule, v.line) for v in violations]


@pytest.mark.parametrize("stem", SANDBOX_HOSTILE)
def test_every_sandbox_hostile_file_exists_and_is_rejected(stem):
    path = ESCAPE_DIR / f"{stem}.py"
    assert path.is_file(), f"Plan 2 loads {stem} as a hostile probe; ship it here"
    assert check_source(path.read_text()), f"{stem} was not rejected"


def test_the_orb_fixture_passes_the_checker():
    assert require_clean(FIXTURE.read_text()) == "OpeningRangeBreakout"
