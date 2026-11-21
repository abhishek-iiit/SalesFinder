import pytest

from conftest import assert_safe_test_database


def test_guard_rejects_non_test_name() -> None:
    with pytest.raises(RuntimeError, match="_test"):
        assert_safe_test_database("postgresql+asyncpg://localhost/salesfinder")


def test_guard_rejects_missing_name() -> None:
    with pytest.raises(RuntimeError, match="_test"):
        assert_safe_test_database("postgresql+asyncpg://localhost")


def test_guard_rejects_real_database_even_if_named_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql+asyncpg://localhost/prod_test")
    with pytest.raises(RuntimeError, match="SALESFINDER_DATABASE_URL"):
        assert_safe_test_database("postgresql+asyncpg://localhost/prod_test")


def test_guard_accepts_test_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql+asyncpg://localhost/salesfinder")
    assert_safe_test_database("postgresql+asyncpg://localhost/salesfinder_test")
