"""Learn a company's email pattern from addresses it publishes and the people named on its
site, then produce at most one candidate address per person. Pure functions; never raise."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from salesfinder.contacts.extract import EmailFound, PersonFound
from salesfinder.contacts.names import name_key, split_name
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email

PATTERNS: tuple[str, ...] = (
    "{first}.{last}",
    "{first}{last}",
    "{f}{last}",
    "{first}",
    "{first}_{last}",
    "{f}.{last}",
    "{last}.{first}",
    "{first}{l}",
    "{last}",
    "{last}{f}",
    "{first}-{last}",
)


@dataclass(frozen=True)
class LearnedPattern:
    pattern: str
    support: int
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    address: str
    status: Literal["published", "pattern_confirmed", "pattern_guess"]
    source: Literal["page", "pattern", "guess"]
    pattern: str | None


def _part(raw: str | None) -> str | None:
    if not raw:
        return None
    key = name_key(raw).replace(" ", "")
    return key or None


def render_address(pattern: str, first: str | None, last: str | None, domain: str) -> str | None:
    """The address ``pattern`` gives for a person, or None if a needed name part is missing."""
    f, la = _part(first), _part(last)
    host = domain.strip().lower()
    if not host:
        return None
    needs_first = "{first}" in pattern or "{f}" in pattern
    needs_last = "{last}" in pattern or "{l}" in pattern
    if (needs_first and f is None) or (needs_last and la is None):
        return None
    local = (
        pattern.replace("{first}", f or "")
        .replace("{last}", la or "")
        .replace("{f}", (f or "")[:1])
        .replace("{l}", (la or "")[:1])
    )
    return f"{local}@{host}" if local else None


def _is_free(domain: str) -> bool:
    return domain.strip().lower() in FREE_MAIL_DOMAINS


def _personal_at(domain: str, emails: Sequence[EmailFound]) -> list[str]:
    host = domain.strip().lower()
    out = {
        e.address.lower()
        for e in emails
        if e.kind == "personal" and domain_of_email(e.address) == host
    }
    return sorted(out)


_MIN_LOCAL = 3
_SINGLE_TOKEN = frozenset({"{first}", "{last}"})


def _unique_match(
    pattern: str, address: str, names: Sequence[tuple[str | None, str | None]], domain: str
) -> tuple[str | None, str | None] | None:
    """The one person whose rendering under ``pattern`` is ``address``; None if zero or several,
    or if the local part is too short to be evidence."""
    if len(address.partition("@")[0]) < _MIN_LOCAL:
        return None
    hits = {
        (_part(f), _part(la))
        for f, la in names
        if render_address(pattern, f, la, domain) == address
    }
    return next(iter(hits)) if len(hits) == 1 else None


def learn_pattern(
    domain: str, people: Sequence[PersonFound], emails: Sequence[EmailFound]
) -> LearnedPattern | None:
    """The pattern best supported by published personal addresses matching named people."""
    if not domain.strip() or _is_free(domain):
        return None
    names = [split_name(p.full_name) for p in people]
    support: Counter[str] = Counter()
    examples: dict[str, set[str]] = {}
    for address in _personal_at(domain, emails):
        for pattern in PATTERNS:
            if _unique_match(pattern, address, names, domain) is not None:
                support[pattern] += 1
                examples.setdefault(pattern, set()).add(address)
    if not support:
        return None
    best = max(support.values())
    leaders = [p for p in PATTERNS if support[p] == best]
    if len(leaders) != 1:
        return None  # conflicting evidence: no confirmed pattern
    pattern = leaders[0]
    if pattern in _SINGLE_TOKEN and best < 2:
        return None
    return LearnedPattern(pattern, best, tuple(sorted(examples[pattern])))


def candidates_for(
    person: PersonFound,
    *,
    domain: str,
    emails: Sequence[EmailFound],
    learned: LearnedPattern | None,
    people: Sequence[PersonFound],
) -> list[Candidate]:
    """At most one candidate: a published address, else learned-pattern, else a guess.

    ``people`` (everyone found on the site) lets a published address count only when it is
    unambiguous among them, and makes a learned-pattern address only a guess when the pattern
    gives the same address for someone else on the site (Amit Shah and Anil Shah under
    ``{f}{last}``)."""
    first, last = split_name(person.full_name)
    names = [split_name(p.full_name) for p in (*people, person)]
    published = set(_personal_at(domain, emails))
    for pattern in PATTERNS:
        address = render_address(pattern, first, last, domain)
        if (
            address is not None
            and address in published
            and _unique_match(pattern, address, names, domain) == (_part(first), _part(last))
        ):
            return [Candidate(address, "published", "page", pattern)]
    if _is_free(domain):
        return []
    if learned is not None:
        address = render_address(learned.pattern, first, last, domain)
        if address is not None:
            owners = {
                (_part(f), _part(la))
                for f, la in names
                if render_address(learned.pattern, f, la, domain) == address
            }
            if len(owners) > 1:
                return [Candidate(address, "pattern_guess", "guess", learned.pattern)]
            return [Candidate(address, "pattern_confirmed", "pattern", learned.pattern)]
    guess = "{first}.{last}" if _part(last) else "{first}"
    address = render_address(guess, first, last, domain)
    if address is None:
        return []
    return [Candidate(address, "pattern_guess", "guess", guess)]
