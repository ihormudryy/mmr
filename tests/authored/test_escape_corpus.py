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


@pytest.mark.parametrize("stem", SANDBOX_HOSTILE)
def test_every_sandbox_hostile_file_exists_and_is_rejected(stem):
    path = ESCAPE_DIR / f"{stem}.py"
    assert path.is_file(), f"Plan 2 loads {stem} as a hostile probe; ship it here"
    assert check_source(path.read_text()), f"{stem} was not rejected"


def test_the_orb_fixture_passes_the_checker():
    assert require_clean(FIXTURE.read_text()) == "OpeningRangeBreakout"
