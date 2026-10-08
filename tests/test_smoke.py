"""Trivial smoke test: the package imports and carries a version."""
import lexaudit


def test_smoke() -> None:
    assert lexaudit.__version__
