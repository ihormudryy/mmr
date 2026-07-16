import platform


def test_runtime_matches_pinned_python():
    assert platform.python_version() == "3.12.13"
