import dns.exception
import pytest

from fakes.dns import Answer, FakeResolver
from salesfinder.config import ContactsConfig
from salesfinder.contacts.dns import MxChecker
from salesfinder.mail.readiness import Report, check_domain, report_from_json

DOMAIN = "send.example.org"
GOOD_SPF = '"v=spf1 include:_spf.google.com ~all"'
GOOD: dict[tuple[str, str], Answer] = {
    (DOMAIN, "TXT"): [(0, GOOD_SPF)],
    (f"google._domainkey.{DOMAIN}", "TXT"): [(0, '"v=DKIM1; k=rsa; p=MIIBIjANBgkq"')],
    (f"_dmarc.{DOMAIN}", "TXT"): [(0, '"v=DMARC1; p=none; rua=mailto:d@send.example.org"')],
    (DOMAIN, "MX"): [(1, "aspmx.l.google.com.")],
}


async def _check(answers: dict[tuple[str, str], Answer], **kwargs: bool) -> Report:
    resolver = FakeResolver(answers)
    mx = MxChecker(ContactsConfig(), resolve=resolver)
    return await check_domain(DOMAIN, dkim_selector="google", resolve=resolver, mx=mx, **kwargs)


def _details(report: Report) -> dict[str, str]:
    return {c.name: c.detail for c in report.checks if not c.ok}


async def test_all_pass() -> None:
    report = await _check(GOOD)
    assert report.ok
    assert [c.name for c in report.checks] == ["spf", "dkim", "dmarc", "mx"]


async def test_txt_chunks_are_joined() -> None:
    answers = {**GOOD, (DOMAIN, "TXT"): [(0, '"v=spf1 include:_spf.goo" "gle.com ~all"')]}
    assert (await _check(answers)).ok


@pytest.mark.parametrize(
    ("key", "answer", "name", "detail"),
    [
        ((DOMAIN, "TXT"), [], "spf", "No SPF record."),
        (
            (DOMAIN, "TXT"),
            [(0, GOOD_SPF), (0, '"v=spf1 -all"')],
            "spf",
            "More than one SPF record.",
        ),
        (
            (DOMAIN, "TXT"),
            [(0, '"v=spf1 include:mailgun.org ~all"')],
            "spf",
            "SPF doesn't include Google (include:_spf.google.com).",
        ),
        (
            (DOMAIN, "TXT"),
            [(0, '"v=spf1 include:_spf.google.com +all"')],
            "spf",
            "SPF allows everyone (+all).",
        ),
        *[
            ((DOMAIN, "TXT"), [(0, f'"{rec}"')], "spf", detail)
            for rec, detail in [
                (
                    "v=spf1 include:_spf.google.com +all ip4:1.2.3.4 ~all",
                    "SPF allows everyone (+all).",
                ),
                ("v=spf1 include:_spf.google.com all", "SPF allows everyone (+all)."),
                ("v=spf1 include:_spf.google.com ?all", "SPF doesn't end with ~all or -all."),
                ("v=spf1 include:_spf.google.com", "SPF doesn't end with ~all or -all."),
                (
                    "v=spf1 include:_spf.google.com redirect=_spf.evil.com",
                    "SPF doesn't end with ~all or -all.",
                ),
                (
                    "v=spf1 include:_spf.google.com.evil ~all",
                    "SPF doesn't include Google (include:_spf.google.com).",
                ),
            ]
        ],
        (
            (f"_dmarc.{DOMAIN}", "TXT"),
            [(0, '"v=DMARC1; p=none"'), (0, '"v=DMARC1; p=reject"')],
            "dmarc",
            "More than one DMARC record.",
        ),
        (
            (f"_dmarc.{DOMAIN}", "TXT"),
            [(0, '"v=DMARC1; p=bogus"')],
            "dmarc",
            "DMARC has no policy (p=).",
        ),
        (
            (f"google._domainkey.{DOMAIN}", "TXT"),
            [],
            "dkim",
            f"No DKIM key at google._domainkey.{DOMAIN}.",
        ),
        (
            (f"google._domainkey.{DOMAIN}", "TXT"),
            [(0, '"v=DKIM1; k=rsa; p="')],
            "dkim",
            f"No DKIM key at google._domainkey.{DOMAIN}.",
        ),
        ((f"_dmarc.{DOMAIN}", "TXT"), [], "dmarc", "No DMARC record."),
        (
            (f"_dmarc.{DOMAIN}", "TXT"),
            [(0, '"v=DMARC1; rua=mailto:d@send.example.org"')],
            "dmarc",
            "DMARC has no policy (p=).",
        ),
    ],
)
async def test_each_failure_has_its_detail(
    key: tuple[str, str], answer: Answer, name: str, detail: str
) -> None:
    report = await _check({**GOOD, key: answer})
    assert not report.ok
    assert _details(report) == {name: detail}


async def test_no_mx() -> None:
    import dns.resolver

    answers = {**GOOD, (DOMAIN, "MX"): dns.resolver.NXDOMAIN()}
    report = await _check(answers)
    assert _details(report) == {"mx": "Domain can't receive mail (no MX)."}


async def test_non_google_host_needs_no_google_spf() -> None:
    answers = {**GOOD, (DOMAIN, "TXT"): [(0, '"v=spf1 include:mailgun.org ~all"')]}
    assert (await _check(answers, google=False)).ok


async def test_dns_timeout_is_not_a_pass() -> None:
    answers = {
        **GOOD,
        (DOMAIN, "TXT"): dns.exception.Timeout(),
        (DOMAIN, "MX"): dns.exception.Timeout(),
    }
    report = await _check(answers)
    assert not report.ok
    assert _details(report)["spf"] == "Couldn't check: DNS timed out."
    assert _details(report)["mx"] == "Couldn't check: DNS timed out."


async def test_report_round_trips_through_json() -> None:
    report = await _check({**GOOD, (f"_dmarc.{DOMAIN}", "TXT"): []})
    assert report_from_json(report.to_json()) == report
    assert not report_from_json({}).ok
    assert not report_from_json({"checks": [{"name": "spf"}]}).ok


async def test_unexpected_resolver_error_is_a_failed_check() -> None:
    answers = {**GOOD, (DOMAIN, "TXT"): RuntimeError("boom"), (DOMAIN, "MX"): RuntimeError("boom")}
    report = await _check(answers)
    assert _details(report)["spf"] == "Couldn't check: DNS server failure."
    assert _details(report)["mx"] == "Couldn't check: DNS server failure."
