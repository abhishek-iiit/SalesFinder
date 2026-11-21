"""Cohorts on the dashboard: the form parser, plain-English summaries, live counts with header
CSRF, CRUD with audit, validation, the name race, and the cohort page."""

import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import FormData

from salesfinder.auth.csrf import csrf_token
from salesfinder.catalog import Catalog, load_catalog
from salesfinder.cohorts import evaluate
from salesfinder.cohorts.evaluate import create_cohort, update_cohort
from salesfinder.cohorts.model import CohortDefinition, Condition
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Cohort, Company, Observation, User
from salesfinder.signals import settings as sig
from salesfinder.signals.detect_buyer import COMPANY_TYPES
from salesfinder.signals.detect_common import CERT_NAMES
from salesfinder.web.cohort_form import (
    COMPANY_TYPE_LABELS,
    choices_for,
    cohort_from_form,
    describe,
    stale_warnings,
    state_from_definition,
)
from web_helpers import audit_actions, csrf_for, login, text_of

SHIPPED = Path(__file__).resolve().parent.parent / "config"
SECRET = "test-secret-key-0123456789-abcdefghijklmnop"  # noqa: S105  (conftest TEST_SECRET_KEY)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(SHIPPED)


def _form(*pairs: tuple[str, str]) -> FormData:
    return FormData(list(pairs))


SLES_CAPB_NOT_DIST = CohortDefinition(
    side="buyer",
    must=[Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles", "capb"])],
    must_not=[Condition(code="COMPANY_TYPE", op="in", values=["distributor"])],
    min_score=40,
)


# --------------------------------------------------------------------------- pure: parser


def test_cohort_from_form(catalog: Catalog) -> None:
    form = _form(
        ("mode.USES_CATALOG_CHEMICAL", "must"),
        ("values.USES_CATALOG_CHEMICAL", "sles"),
        ("values.USES_CATALOG_CHEMICAL", "capb"),
        ("mode.COMPANY_TYPE", "must_not"),
        ("values.COMPANY_TYPE", "distributor"),
        ("min_score", "40"),
        # Ignored: unknown names, other-side signals, ignored rows' inputs, the name/csrf fields.
        ("mode.NOPE", "must"),
        ("mode.EXPORTS", "must"),
        ("values.QUALITY_CERTS", "GMP"),
        ("mode.QUALITY_CERTS", "ignore"),
        ("name", "SLES users"),
        ("csrf_token", "x"),
        ("whatever", "1"),
    )
    assert cohort_from_form(form, "buyer", catalog=catalog) == SLES_CAPB_NOT_DIST


def test_cohort_from_form_kinds(catalog: Catalog) -> None:
    buyer = cohort_from_form(
        _form(
            ("mode.USES_CATALOG_CHEMICAL", "must"),  # nothing ticked -> any chemical at all
            ("mode.NEAR_WAREHOUSE", "must"),
            ("number.NEAR_WAREHOUSE", " 300 "),
            ("mode.US_LOCATION", "must_not"),
            ("number.US_LOCATION", "5"),  # no number input for this signal: ignored
            ("mode.HIRING_PROCUREMENT", "must"),
            ("mode.INDUSTRY_MATCH", "must"),
            ("values.INDUSTRY_MATCH", "personal_care"),
            ("values.INDUSTRY_MATCH", "personal_care"),  # duplicates collapse
            ("mode.QUALITY_CERTS", "must"),
            ("values.QUALITY_CERTS", "ISO 9001"),
            ("min_score", ""),
            ("include_disqualified", "1"),
        ),
        "buyer",
        catalog=catalog,
    )
    assert buyer == CohortDefinition(
        side="buyer",
        must=[
            Condition(code="INDUSTRY_MATCH", op="contains_any", values=["personal_care"]),
            Condition(code="USES_CATALOG_CHEMICAL", op="present"),
            Condition(code="NEAR_WAREHOUSE", op="lte", field="miles", number=300),
            Condition(code="HIRING_PROCUREMENT", op="present"),
            Condition(code="QUALITY_CERTS", op="contains_any", values=["ISO 9001"]),
        ],
        must_not=[Condition(code="US_LOCATION", op="present")],
        include_disqualified=True,
    )
    supplier = cohort_from_form(
        _form(
            ("mode.MANUFACTURER_NOT_TRADER", "must"),
            ("values.MANUFACTURER_NOT_TRADER", "manufacturer"),
            ("mode.EXPORTS_TO_US", "must"),
            ("mode.STATED_CAPACITY", "must"),
            ("number.STATED_CAPACITY", "1000"),
            ("mode.STATED_CAPACITY_EXTRA", "must"),
            ("mode.REGION", "must_not"),
            ("mode.USES_CATALOG_CHEMICAL", "must"),  # a buyer signal on the supplier form
        ),
        "supplier",
        catalog=catalog,
    )
    assert supplier == CohortDefinition(
        side="supplier",
        must=[
            Condition(code="MANUFACTURER_NOT_TRADER", op="in", values=["manufacturer"]),
            Condition(code="EXPORTS_TO_US", op="present"),
            Condition(code="STATED_CAPACITY", op="gte", field="mt_per_year", number=1000),
        ],
        must_not=[Condition(code="REGION", op="present")],
    )
    # An empty form is a valid "everyone" cohort.
    assert cohort_from_form(_form(), "buyer", catalog=catalog) == CohortDefinition(side="buyer")


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        ([("mode.COMPANY_TYPE", "maybe")], "isn't a valid choice"),
        ([("mode.COMPANY_TYPE", "must"), ("values.COMPANY_TYPE", "pirate")], "isn't valid"),
        (
            [("mode.USES_CATALOG_CHEMICAL", "must"), ("values.USES_CATALOG_CHEMICAL", "nope")],
            "isn't valid",
        ),
        ([("mode.NEAR_WAREHOUSE", "must"), ("number.NEAR_WAREHOUSE", "far")], "number"),
        ([("mode.NEAR_WAREHOUSE", "must"), ("number.NEAR_WAREHOUSE", "nan")], "number"),
        ([("mode.NEAR_WAREHOUSE", "must"), ("number.NEAR_WAREHOUSE", "inf")], "number"),
        ([("mode.NEAR_WAREHOUSE", "must"), ("number.NEAR_WAREHOUSE", "-5")], "number"),
        ([("min_score", "abc")], "Minimum score"),
        ([("min_score", "101")], "Minimum score"),
        ([("min_score", "-1")], "Minimum score"),
        ([("min_score", "4.5")], "Minimum score"),
        ([("include_disqualified", "maybe")], "disqualified"),
        ([("include_unprofiled", "maybe")], "couldn't read yet"),
    ],
)
def test_cohort_from_form_rejects(
    catalog: Catalog, pairs: list[tuple[str, str]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        cohort_from_form(_form(*pairs), "buyer", catalog=catalog)


def test_cohort_from_form_rejects_bad_side(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="side"):
        cohort_from_form(_form(), "both", catalog=catalog)


def test_choice_vocabularies_cover_detectors(catalog: Catalog) -> None:
    assert set(COMPANY_TYPE_LABELS) == set(COMPANY_TYPES)
    assert [v for v, _ in choices_for("QUALITY_CERTS", catalog) or []] == list(CERT_NAMES)
    assert [v for v, _ in choices_for("SUPPLIER_CERTS", catalog) or []] == list(CERT_NAMES)
    products = choices_for("USES_CATALOG_CHEMICAL", catalog) or []
    assert ("sles", "Sodium Laureth Sulfate (SLES 70%)") in products
    assert choices_for("US_LOCATION", catalog) is None


# --------------------------------------------------------------------------- pure: summary


def test_describe(catalog: Catalog) -> None:
    assert describe(SLES_CAPB_NOT_DIST, catalog) == (
        "Uses Sodium Laureth Sulfate (SLES 70%) or Cocamidopropyl Betaine (CAPB)"
        " · not a distributor · score ≥ 40"
    )
    assert describe(CohortDefinition(side="buyer"), catalog) == "All buyer companies"
    assert (
        describe(CohortDefinition(side="supplier", include_disqualified=True), catalog)
        == "All supplier companies · including disqualified"
    )
    buyer = CohortDefinition(
        side="buyer",
        must=[
            Condition(
                code="INDUSTRY_MATCH", op="contains_any", values=["personal_care", "home_care"]
            ),
            Condition(code="NEAR_WAREHOUSE", op="lte", field="miles", number=300),
            Condition(code="COMPANY_TYPE", op="in", values=["manufacturer", "private_label"]),
            Condition(code="USES_CATALOG_CHEMICAL", op="contains_all", values=["sles", "gone"]),
        ],
        must_not=[
            Condition(code="USES_CATALOG_CHEMICAL", op="present"),
            Condition(code="QUALITY_CERTS", op="contains_any", values=["GMP", "Kosher", "Halal"]),
        ],
    )
    assert describe(buyer, catalog) == (
        "Works in Personal Care or Home Care & Industrial Cleaning"
        " · within 300 miles of an Elchemy warehouse"
        " · a manufacturer or private-label maker"
        " · uses Sodium Laureth Sulfate (SLES 70%) and gone"
        " · uses none of our chemicals"
        " · doesn't have GMP, Kosher or Halal"
    )
    supplier = CohortDefinition(
        side="supplier",
        must=[
            Condition(code="EXPORTS_TO_US", op="present"),
            Condition(code="STATED_CAPACITY", op="gte", field="mt_per_year", number=1200.5),
            Condition(code="MANUFACTURER_NOT_TRADER", op="in", values=["manufacturer"]),
        ],
        must_not=[Condition(code="EXPORTS", op="is_true")],
        min_score=0,
    )
    assert describe(supplier, catalog) == (
        "Exports to the US · capacity at least 1,200.5 MT/year · a manufacturer"
        " · doesn't export · score ≥ 0"
    )


# --------------------------------------------------------------------------- web helpers


async def _company(
    db: AsyncSession,
    name: str,
    *,
    side: str = "buyer",
    score: int | None = None,
    status: str = "new",
    profile_status: str = "ok",
    obs: dict[str, Any] | None = None,
) -> Company:
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    c = Company(
        side=side,
        name=name,
        domain=f"{slug}.com",
        website_url=f"https://{slug}.com",
        status=status,
        profile_status=profile_status,
        score=score,
    )
    db.add(c)
    await db.flush()
    for code, value in (obs or {}).items():
        db.add(Observation(company_id=c.id, code=code, value=value, is_latest=True))
    await db.flush()
    return c


async def _confirm(db: AsyncSession, actor: User, *codes: str) -> None:
    for code in codes:
        await sig.set_state(db, code, "confirmed", actor=actor)


SLES = {"USES_CATALOG_CHEMICAL": ["sles"]}
SLES_FORM = {
    "side": "buyer",
    "mode.USES_CATALOG_CHEMICAL": "must",
    "values.USES_CATALOG_CHEMICAL": "sles",
    "mode.COMPANY_TYPE": "must_not",
    "values.COMPANY_TYPE": "distributor",
}


async def _three_buyers(db: AsyncSession, member: User) -> None:
    await _confirm(db, member, "USES_CATALOG_CHEMICAL", "COMPANY_TYPE")
    await _company(db, "Alpha Soap", score=50, obs={**SLES, "COMPANY_TYPE": "manufacturer"})
    await _company(db, "Bravo Suds", score=80, obs=SLES)
    await _company(db, "Charlie Dist", score=90, obs={**SLES, "COMPANY_TYPE": "distributor"})
    await _company(db, "Excluded Co", score=99, status="excluded", obs=SLES)
    await db.commit()


# --------------------------------------------------------------------------- web


async def test_new_form_shows_only_confirmed(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    anon = await client.get("/cohorts/new?side=buyer")
    assert anon.status_code == 303 and anon.headers["location"].startswith("/login")
    await _confirm(db, member, "USES_CATALOG_CHEMICAL", "COMPANY_TYPE", "EXPORTS")
    await db.commit()
    await login(client, member.email, password)

    resp = await client.get("/cohorts/new?side=buyer")
    assert resp.status_code == 200
    page = text_of(resp)
    assert 'name="mode.USES_CATALOG_CHEMICAL"' in page
    assert 'name="mode.COMPANY_TYPE"' in page
    assert 'name="values.USES_CATALOG_CHEMICAL" value="sles"' in page
    assert 'name="values.COMPANY_TYPE" value="distributor"' in page
    # Unconfirmed buyer signals are listed greyed out with a link to /signals, not editable.
    assert 'name="mode.QUALITY_CERTS"' not in page
    assert 'name="number.NEAR_WAREHOUSE"' not in page
    unconfirmed = page.split('class="unconfirmed')[1].split("</ul>")[0]
    assert "Quality certifications" in unconfirmed and 'href="/signals"' in unconfirmed
    # Supplier signals never appear on a buyer form.
    assert "mode.EXPORTS" not in page and "Exports to the US" not in page
    # Live count wiring: HTMX on the form, token via hx-headers built server-side.
    assert 'hx-post="/cohorts/count"' in page
    assert 'hx-trigger="change delay:300ms"' in page
    token = re.search(r'<meta name="csrf-token" content="([0-9a-f]+)">', page)
    assert token is not None
    # JSON in a double-quoted attribute, quotes entity-escaped by autoescape.
    assert f'hx-headers="{{&#34;X-CSRF-Token&#34;: &#34;{token.group(1)}&#34;}}"' in resp.text
    assert "<script>" not in resp.text
    assert "0 companies match" in page

    supplier = text_of(await client.get("/cohorts/new?side=supplier"))
    assert 'name="mode.EXPORTS"' in supplier and 'name="mode.COMPANY_TYPE"' not in supplier
    assert (await client.get("/cohorts/new?side=both")).status_code == 400


async def test_csrf_meta_only_when_signed_in(client: httpx.AsyncClient) -> None:
    assert 'name="csrf-token"' not in (await client.get("/login")).text


async def test_live_count_with_header_csrf(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await _three_buyers(db, member)
    await login(client, member.email, password)
    token = await csrf_for(client, "/cohorts/new?side=buyer")
    hx = {"HX-Request": "true", "X-CSRF-Token": token}

    resp = await client.post("/cohorts/count", data=SLES_FORM, headers=hx)
    assert resp.status_code == 200
    assert "2 companies match" in text_of(resp)
    assert "<html" not in resp.text  # a fragment

    narrow = {**SLES_FORM, "min_score": "60"}
    assert "1 company matches" in text_of(
        await client.post("/cohorts/count", data=narrow, headers=hx)
    )
    # The form field still works on its own.
    form_token = await client.post("/cohorts/count", data={**SLES_FORM, "csrf_token": token})
    assert "2 companies match" in text_of(form_token)
    # A bad selection is reported in the fragment, not a 500.
    bad = await client.post("/cohorts/count", data={**SLES_FORM, "min_score": "x"}, headers=hx)
    assert bad.status_code == 200 and "Minimum score" in text_of(bad)
    assert (
        await client.post("/cohorts/count", data={**SLES_FORM, "side": "x"}, headers=hx)
    ).status_code == 400

    # Without a token, with a forged one, or with another session's token: 403.
    assert (
        await client.post("/cohorts/count", data=SLES_FORM, headers={"HX-Request": "true"})
    ).status_code == 403
    for forged in ("nope", csrf_token(SECRET, uuid.uuid4()), ""):
        resp = await client.post(
            "/cohorts/count", data=SLES_FORM, headers={"HX-Request": "true", "X-CSRF-Token": forged}
        )
        assert resp.status_code == 403, forged
    # A bad form field doesn't spoil a good header (either one may carry the token).
    both = await client.post("/cohorts/count", data={**SLES_FORM, "csrf_token": "x"}, headers=hx)
    assert both.status_code == 200


async def test_header_csrf_accepted_on_existing_forms(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    token = await csrf_for(client, "/signals")
    resp = await client.post("/signals/confirm-free", headers={"X-CSRF-Token": token})
    assert resp.status_code == 303
    assert (await client.post("/signals/confirm-free")).status_code == 403


async def test_create_view_edit_delete(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await _three_buyers(db, member)
    member_id = member.id
    await login(client, member.email, password)
    token = await csrf_for(client, "/cohorts/new?side=buyer")

    resp = await client.post(
        "/cohorts", data={**SLES_FORM, "name": "  SLES users ", "csrf_token": token}
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    match = re.fullmatch(r"/cohorts/([0-9a-f-]{36})\?msg=saved", location)
    assert match, location
    cohort_id = uuid.UUID(match.group(1))

    page = text_of(await client.get(location))
    assert "Cohort saved." in page
    assert "SLES users" in page
    assert "Uses Sodium Laureth Sulfate (SLES 70%) · not a distributor" in page
    assert "2 companies match" in page
    assert page.index("Bravo Suds") < page.index("Alpha Soap")  # score desc
    assert "Charlie Dist" not in page and "Excluded Co" not in page
    assert re.search(r'href="/companies/[0-9a-f-]{36}">Bravo Suds</a>', page)

    listing = text_of(await client.get("/cohorts"))
    row = listing.split('data-cohort="SLES users"')[1].split("</tr>")[0]
    assert '<td class="num">2</td>' in row and "Mira Member" in row and "Buyer" in row
    assert f'href="/cohorts/{cohort_id}"' in row

    edit = text_of(await client.get(f"/cohorts/{cohort_id}/edit"))
    assert 'value="SLES users"' in edit
    assert 'name="values.USES_CATALOG_CHEMICAL" value="sles" checked' in edit
    assert re.search(r'<option value="must_not" selected>', edit)
    assert f'action="/cohorts/{cohort_id}"' in edit

    upd = await client.post(
        f"/cohorts/{cohort_id}",
        # A side in the form can't move a cohort to the other side.
        data={**SLES_FORM, "side": "supplier", "name": "SLES makers", "min_score": "60"},
        headers={"X-CSRF-Token": token},
    )
    assert upd.status_code == 303 and upd.headers["location"] == f"/cohorts/{cohort_id}?msg=saved"
    page = text_of(await client.get(upd.headers["location"]))
    assert "SLES makers" in page and "score ≥ 60" in page and "1 company matches" in page

    dele = await client.post(f"/cohorts/{cohort_id}/delete", data={"csrf_token": token})
    assert dele.status_code == 303 and dele.headers["location"] == "/cohorts?msg=deleted"
    assert "Cohort deleted." in text_of(await client.get(dele.headers["location"]))
    assert (await client.get(f"/cohorts/{cohort_id}")).status_code == 404
    assert (await client.get(f"/cohorts/{cohort_id}/edit")).status_code == 404
    gone = await client.post(f"/cohorts/{cohort_id}/delete", data={"csrf_token": token})
    assert gone.status_code == 404
    missing = await client.post(f"/cohorts/{cohort_id}", data={**SLES_FORM, "csrf_token": token})
    assert missing.status_code == 404

    actions = await audit_actions(db)
    assert [a for a in actions if a.startswith("cohort.")] == [
        "cohort.created",
        "cohort.updated",
        "cohort.deleted",
    ]
    logs = {
        log.action: log
        for log in await db.scalars(select(AuditLog).where(AuditLog.action.like("cohort.%")))
    }
    created = logs["cohort.created"]
    assert created.actor_id == member_id and created.target_id == str(cohort_id)
    assert created.target_type == "cohort"
    assert created.details["name"] == "SLES users" and created.details["side"] == "buyer"
    expected = SLES_CAPB_NOT_DIST.model_copy(
        update={
            "must": [Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"])],
            "min_score": None,
        }
    )
    assert created.details["definition"] == expected.model_dump(mode="json")
    updated = logs["cohort.updated"]
    assert updated.details["name"] == "SLES makers" and updated.details["side"] == "buyer"
    assert updated.details["definition"]["min_score"] == 60
    assert logs["cohort.deleted"].details["name"] == "SLES makers"

    # CSRF is required on every write.
    assert (await client.post("/cohorts", data={**SLES_FORM, "name": "x"})).status_code == 403
    assert (await db.scalar(select(Cohort))) is None


async def test_duplicate_name_and_invalid(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await _three_buyers(db, member)
    await login(client, member.email, password)
    token = await csrf_for(client, "/cohorts/new?side=buyer")
    data = {**SLES_FORM, "csrf_token": token, "min_score": "30"}
    assert (await client.post("/cohorts", data={**data, "name": "SLES users"})).status_code == 303

    dup = await client.post("/cohorts", data={**data, "name": "sles USERS"})
    assert dup.status_code == 400
    page = text_of(dup)
    assert "A cohort with that name already exists." in page
    # Selections are kept.
    assert 'value="sles USERS"' in page
    assert 'name="values.USES_CATALOG_CHEMICAL" value="sles" checked' in page
    assert 'name="values.COMPANY_TYPE" value="distributor" checked' in page
    assert 'name="min_score" value="30"' in page

    bad = await client.post("/cohorts", data={**data, "name": "New", "min_score": "abc"})
    assert bad.status_code == 400
    page = text_of(bad)
    assert "Minimum score must be a whole number from 0 to 100." in page
    assert 'name="min_score" value="abc"' in page and 'value="New"' in page
    assert 'name="values.USES_CATALOG_CHEMICAL" value="sles" checked' in page

    for name in ("   ", "x" * 121, "nul\x00name"):
        resp = await client.post("/cohorts", data={**data, "name": name})
        assert resp.status_code == 400, name
    assert "1 to 120 characters" in text_of(
        await client.post("/cohorts", data={**data, "name": " "})
    )
    assert (
        await client.post("/cohorts", data={**data, "side": "x", "name": "y"})
    ).status_code == 400

    # Renaming onto an existing name is refused too.
    other = await client.post("/cohorts", data={**data, "name": "Other"})
    other_id = other.headers["location"].split("/")[2].split("?")[0]
    clash = await client.post(f"/cohorts/{other_id}", data={**data, "name": "SLES users"})
    assert clash.status_code == 400
    assert "A cohort with that name already exists." in text_of(clash)
    assert f'action="/cohorts/{other_id}"' in clash.text

    db.expire_all()
    assert sorted(await db.scalars(select(Cohort.name))) == ["Other", "SLES users"]
    actions = [a for a in await audit_actions(db) if a.startswith("cohort.")]
    assert actions == ["cohort.created", "cohort.created"]


async def _no_precheck(*_: object, **__: object) -> bool:
    return False


async def test_name_race_maps_to_400(
    client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-check passes but another request inserted the same name first: the unique
    constraint fires inside a savepoint and the user gets the friendly 400, not a 500."""
    await _three_buyers(db, member)
    await login(client, member.email, password)
    token = await csrf_for(client, "/cohorts/new?side=buyer")
    data = {**SLES_FORM, "csrf_token": token}
    assert (await client.post("/cohorts", data={**data, "name": "Taken"})).status_code == 303
    second = await client.post("/cohorts", data={**data, "name": "Second"})
    second_id = second.headers["location"].split("/")[2].split("?")[0]

    monkeypatch.setattr(evaluate, "_name_taken", _no_precheck)
    race = await client.post("/cohorts", data={**data, "name": "Taken"})
    assert race.status_code == 400
    assert "A cohort with that name already exists." in text_of(race)
    assert "2 companies match" in text_of(race)  # the session survived the failed insert
    rename = await client.post(f"/cohorts/{second_id}", data={**data, "name": "Taken"})
    assert rename.status_code == 400
    assert "A cohort with that name already exists." in text_of(rename)

    db.expire_all()
    assert sorted(await db.scalars(select(Cohort.name))) == ["Second", "Taken"]
    actions = [a for a in await audit_actions(db) if a.startswith("cohort.")]
    assert actions == ["cohort.created", "cohort.created"]


async def test_name_race_in_service(
    db: AsyncSession, engine: AsyncEngine, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    defn = CohortDefinition(side="buyer")
    keep = await create_cohort(db, name="Keep", definition=defn, actor=admin)
    await db.commit()
    keep_id = keep.id
    # Another session commits "Racer" after this session's pre-check would have passed.
    monkeypatch.setattr(evaluate, "_name_taken", _no_precheck)
    async with make_session_factory(engine)() as other:
        await create_cohort(other, name="Racer", definition=defn, actor=admin)
        await other.commit()
    with pytest.raises(ValueError, match="already exists"):
        await create_cohort(db, name="Racer", definition=defn, actor=admin)
    with pytest.raises(ValueError, match="already exists"):
        await update_cohort(db, keep_id, name="Racer", definition=defn, actor=admin)
    # The outer transaction is still usable.
    assert sorted(await db.scalars(select(Cohort.name))) == ["Keep", "Racer"]
    with pytest.raises(ValueError, match="control characters"):
        await create_cohort(db, name="bad\x00", definition=defn, actor=admin)


async def test_cohort_page_paginates_and_excludes_excluded(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession, admin: User
) -> None:
    await _confirm(db, member, "USES_CATALOG_CHEMICAL")
    for i in range(55):
        await _company(db, f"Buyer {i:02d}", score=i, obs=SLES)
    await _company(db, "Excluded Top", score=100, status="excluded", obs=SLES)
    await _company(db, "Supplier Top", side="supplier", score=100)
    defn = CohortDefinition(
        side="buyer",
        must=[Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"])],
    )
    cohort = await create_cohort(db, name="<b>SLES</b>", definition=defn, actor=admin)
    await db.commit()
    cohort_id = cohort.id
    await login(client, member.email, password)

    first = await client.get(f"/cohorts/{cohort_id}")
    assert first.status_code == 200
    page = text_of(first)
    assert "<b>SLES</b>" not in first.text and "&lt;b&gt;SLES&lt;/b&gt;" in first.text
    assert "55 companies match" in page
    names = re.findall(r'href="/companies/[0-9a-f-]{36}">([^<]+)</a>', page)
    assert len(names) == 50 and names[0] == "Buyer 54" and names[-1] == "Buyer 05"
    assert "Excluded Top" not in page and "Supplier Top" not in page
    assert f'href="/cohorts/{cohort_id}?page=2"' in page

    second = text_of(await client.get(f"/cohorts/{cohort_id}?page=2"))
    names = re.findall(r'href="/companies/[0-9a-f-]{36}">([^<]+)</a>', second)
    assert names == ["Buyer 04", "Buyer 03", "Buyer 02", "Buyer 01", "Buyer 00"]
    assert f'href="/cohorts/{cohort_id}?page=1"' in second

    assert (await client.get(f"/cohorts/{cohort_id}?page=0")).status_code == 400
    assert (await client.get(f"/cohorts/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/cohorts/not-a-uuid")).status_code in (400, 404)
    listing = await client.get("/cohorts")
    assert "&lt;b&gt;SLES&lt;/b&gt;" in listing.text and "<b>SLES</b>" not in listing.text


async def test_edit_keeps_conditions_on_unconfirmed_signals(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession, admin: User
) -> None:
    await _confirm(db, member, "USES_CATALOG_CHEMICAL")
    defn = CohortDefinition(
        side="buyer",
        must=[
            Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"]),
            Condition(code="QUALITY_CERTS", op="contains_any", values=["GMP"]),
        ],
    )
    cohort = await create_cohort(db, name="Certs", definition=defn, actor=admin)
    await db.commit()
    cohort_id = cohort.id
    await login(client, member.email, password)
    edit = text_of(await client.get(f"/cohorts/{cohort_id}/edit"))
    # Not editable, but carried through a save so it isn't silently dropped.
    assert 'type="hidden" name="mode.QUALITY_CERTS" value="must"' in edit
    assert 'type="hidden" name="values.QUALITY_CERTS" value="GMP"' in edit
    assert 'name="mode.QUALITY_CERTS" value="must"' in edit
    token = await csrf_for(client, f"/cohorts/{cohort_id}/edit")
    resp = await client.post(
        f"/cohorts/{cohort_id}",
        data={
            "csrf_token": token,
            "name": "Certs",
            "mode.USES_CATALOG_CHEMICAL": "must",
            "values.USES_CATALOG_CHEMICAL": "sles",
            "mode.QUALITY_CERTS": "must",
            "values.QUALITY_CERTS": "GMP",
        },
    )
    assert resp.status_code == 303
    db.expire_all()
    saved = await db.get(Cohort, cohort_id)
    assert saved is not None
    assert CohortDefinition.model_validate(saved.definition) == defn


# --------------------------------------------------------------------------- final review fixes


def test_include_unprofiled_form_round_trip(catalog: Catalog) -> None:
    defn = cohort_from_form(_form(("include_unprofiled", "1")), "buyer", catalog=catalog)
    assert defn == CohortDefinition(side="buyer", include_unprofiled=True)
    assert cohort_from_form(_form(), "buyer", catalog=catalog).include_unprofiled is False
    assert state_from_definition("x", defn).include_unprofiled is True
    assert describe(defn, catalog) == (
        "All buyer companies · including companies we couldn't read yet"
    )


def test_stale_warnings(catalog: Catalog) -> None:
    confirmed = frozenset({"USES_CATALOG_CHEMICAL"})
    assert stale_warnings(SLES_CAPB_NOT_DIST, confirmed, catalog) == [
        "Not collected now: “not a distributor” currently excludes nobody."
    ]
    assert stale_warnings(SLES_CAPB_NOT_DIST, frozenset({"COMPANY_TYPE"}), catalog) == [
        "Not collected now: “uses Sodium Laureth Sulfate (SLES 70%) or Cocamidopropyl Betaine"
        " (CAPB)” currently matches nobody."
    ]
    absent = CohortDefinition(
        side="buyer",
        must=[Condition(code="QUALITY_CERTS", op="absent")],
        must_not=[Condition(code="HIRING_PROCUREMENT", op="is_false")],
    )
    assert stale_warnings(absent, frozenset(), catalog) == [
        "Not collected now: “no quality certification” currently has no effect.",
        "Not collected now: “hiring in purchasing” currently excludes nobody.",
    ]
    absent_not = CohortDefinition(
        side="buyer", must_not=[Condition(code="QUALITY_CERTS", op="absent")]
    )
    assert stale_warnings(absent_not, frozenset(), catalog) == [
        "Not collected now: “has a quality certification” currently excludes everybody."
    ]
    every = frozenset({"USES_CATALOG_CHEMICAL", "COMPANY_TYPE"})
    assert stale_warnings(SLES_CAPB_NOT_DIST, every, catalog) == []


async def test_pages_warn_about_unconfirmed_signals_and_show_profile_status(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession, admin: User
) -> None:
    await _confirm(db, member, "USES_CATALOG_CHEMICAL")  # COMPANY_TYPE is not confirmed
    await _company(db, "Read Co", score=10, obs=SLES)
    await _company(db, "Partly Co", score=5, profile_status="partial", obs=SLES)
    await _company(db, "Waiting Co", profile_status="pending", obs=SLES)
    await _company(db, "Gone Co", profile_status="no_site", obs=SLES)
    defn = CohortDefinition(
        side="buyer",
        must=[Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"])],
        must_not=[Condition(code="COMPANY_TYPE", op="in", values=["distributor"])],
    )
    cohort = await create_cohort(db, name="Stale", definition=defn, actor=admin)
    wide = await create_cohort(
        db,
        name="Wide",
        definition=defn.model_copy(update={"include_unprofiled": True}),
        actor=admin,
    )
    await db.commit()
    cohort_id, wide_id = cohort.id, wide.id
    await login(client, member.email, password)
    warning = "Not collected now: “not a distributor” currently excludes nobody."

    page = text_of(await client.get(f"/cohorts/{cohort_id}"))
    assert warning in page
    assert "2 companies match" in page
    names = re.findall(r'href="/companies/[0-9a-f-]{36}">([^<]+)</a>', page)
    assert names == ["Read Co", "Partly Co"]
    assert '<td class="profile-status">Profiled</td>' in page
    assert '<td class="profile-status">Partly profiled</td>' in page
    assert "nor do companies whose website we haven't read yet" in page

    wide_page = text_of(await client.get(f"/cohorts/{wide_id}"))
    assert "4 companies match" in wide_page
    assert '<td class="profile-status">Waiting</td>' in wide_page
    assert '<td class="profile-status">No website</td>' in wide_page
    assert "nor do companies" not in wide_page

    listing = text_of(await client.get("/cohorts"))
    assert listing.count(warning) == 2

    await _confirm(db, member, "COMPANY_TYPE")
    await db.commit()
    assert warning not in text_of(await client.get(f"/cohorts/{cohort_id}"))
    assert warning not in text_of(await client.get("/cohorts"))


async def test_form_offers_include_unprofiled(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession, admin: User
) -> None:
    cohort = await create_cohort(
        db,
        name="Wide",
        definition=CohortDefinition(side="buyer", include_unprofiled=True),
        actor=admin,
    )
    await db.commit()
    cohort_id = cohort.id
    await login(client, member.email, password)
    new = text_of(await client.get("/cohorts/new?side=buyer"))
    assert '<input type="checkbox" name="include_unprofiled" value="1">' in new
    assert "Include companies we couldn't read yet" in new
    edit = text_of(await client.get(f"/cohorts/{cohort_id}/edit"))
    assert '<input type="checkbox" name="include_unprofiled" value="1" checked>' in edit


def test_require_sendable_contact_form_round_trip(catalog: Catalog) -> None:
    defn = cohort_from_form(_form(("require_sendable_contact", "1")), "buyer", catalog=catalog)
    assert defn == CohortDefinition(side="buyer", require_sendable_contact=True)
    assert cohort_from_form(_form(), "buyer", catalog=catalog).require_sendable_contact is False
    assert state_from_definition("x", defn).require_sendable_contact is True
    assert "has a sendable contact" in describe(defn, catalog)
    assert "sendable" not in describe(CohortDefinition(side="buyer"), catalog)
    with pytest.raises(ValueError, match="valid choice"):
        cohort_from_form(_form(("require_sendable_contact", "maybe")), "buyer", catalog=catalog)


async def test_form_offers_require_sendable_contact(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession, admin: User
) -> None:
    cohort = await create_cohort(
        db,
        name="Reachable",
        definition=CohortDefinition(side="buyer", require_sendable_contact=True),
        actor=admin,
    )
    await db.commit()
    cohort_id = cohort.id
    await login(client, member.email, password)
    new = text_of(await client.get("/cohorts/new?side=buyer"))
    assert '<input type="checkbox" name="require_sendable_contact" value="1">' in new
    assert "Only companies with a sendable contact" in new
    edit = text_of(await client.get(f"/cohorts/{cohort_id}/edit"))
    assert '<input type="checkbox" name="require_sendable_contact" value="1" checked>' in edit
    assert "has a sendable contact" in text_of(await client.get(f"/cohorts/{cohort_id}"))
