import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import User
from salesfinder.signals import settings as sig
from salesfinder.signals.catalog import SIGNALS, by_code, for_side


async def test_defaults_proposed(db: AsyncSession) -> None:
    states = await sig.states(db)
    assert len(SIGNALS) == 14
    assert set(states) == {s.code for s in SIGNALS}
    assert set(states.values()) == {"proposed"}
    assert await sig.confirmed_codes(db) == frozenset()


async def test_set_and_filter_by_side(db: AsyncSession, admin: User) -> None:
    await sig.set_state(db, "SUPPLIER_CERTS", "confirmed", actor=admin)
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=admin)
    assert await sig.confirmed_codes(db, "buyer") == {"QUALITY_CERTS"}
    assert await sig.confirmed_codes(db, "supplier") == {"SUPPLIER_CERTS"}
    assert await sig.confirmed_codes(db) == {"QUALITY_CERTS", "SUPPLIER_CERTS"}
    await sig.set_state(db, "QUALITY_CERTS", "skipped", actor=None)
    assert (await sig.states(db))["QUALITY_CERTS"] == "skipped"
    assert await sig.confirmed_codes(db, "buyer") == frozenset()


async def test_invalid_code_or_state(db: AsyncSession, admin: User) -> None:
    with pytest.raises(ValueError, match="NOPE"):
        await sig.set_state(db, "NOPE", "confirmed", actor=admin)
    with pytest.raises(ValueError, match="maybe"):
        await sig.set_state(db, "EXPORTS", "maybe", actor=admin)


async def test_confirm_free(db: AsyncSession, admin: User) -> None:
    assert await sig.confirm_free(db, actor=admin) == 14
    assert await sig.confirm_free(db, actor=admin) == 0
    assert len(await sig.confirmed_codes(db)) == 14


def test_catalog_shape() -> None:
    assert len(for_side("buyer")) == 7 and len(for_side("supplier")) == 7
    assert by_code("US_LOCATION").value_kind == "object"
    with pytest.raises(KeyError):
        by_code("NOPE")
    assert all(s.label and s.description.endswith(".") for s in SIGNALS)
