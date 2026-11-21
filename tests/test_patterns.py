"""Email pattern learning and candidate addresses."""

from salesfinder.contacts.extract import EmailFound, PersonFound
from salesfinder.contacts.patterns import (
    Candidate,
    LearnedPattern,
    candidates_for,
    learn_pattern,
    render_address,
)


def person(name: str) -> PersonFound:
    return PersonFound(name, "Sales Manager", "sales", "https://acme.com/team", name)


def email(address: str, kind: str = "personal") -> EmailFound:
    assert kind in ("personal", "role")
    return EmailFound(address, kind, "https://acme.com/contact", address)  # type: ignore[arg-type]


PEOPLE = [person("Priya Nair"), person("Anil Mehta")]


def test_render() -> None:
    assert render_address("{f}{last}", "Priya", "Nair", "acme.com") == "pnair@acme.com"
    assert render_address("{first}.{last}", "Priya", None, "ACME.com") is None
    assert render_address("{first}", "Priya", None, "ACME.com") == "priya@acme.com"
    assert render_address("{last}", None, "Nair", "acme.com") == "nair@acme.com"
    assert render_address("{first}{l}", "Priya", "Nair", "acme.com") == "priyan@acme.com"


def test_render_odd_names() -> None:
    assert render_address("{first}.{last}", "Mary-Jane", "Smith", "acme.com") == (
        "maryjane.smith@acme.com"
    )
    assert render_address("{first}.{last}", "José", "Álvarez", "acme.com") == (
        "jose.alvarez@acme.com"
    )
    assert render_address("{first}.{last}", "", "Nair", "acme.com") is None
    assert render_address("{first}.{last}", "12", "Nair", "acme.com") is None


def test_learn_single() -> None:
    learned = learn_pattern("acme.com", PEOPLE, [email("priya.nair@acme.com")])
    assert learned == LearnedPattern("{first}.{last}", 1, ("priya.nair@acme.com",))


def test_learn_support_counts_and_examples_sorted() -> None:
    emails = [email("anil.mehta@acme.com"), email("priya.nair@acme.com"), email("amehta@acme.com")]
    learned = learn_pattern("acme.com", PEOPLE, emails)
    assert learned == LearnedPattern(
        "{first}.{last}", 2, ("anil.mehta@acme.com", "priya.nair@acme.com")
    )


def test_learn_tie_and_conflict() -> None:
    # Different patterns supported 1-1: conflicting evidence, nothing confirmed.
    emails = [email("amehta@acme.com"), email("priya.nair@acme.com")]
    assert learn_pattern("acme.com", PEOPLE, emails) is None


def test_learn_single_token_needs_two() -> None:
    mehta = [person("Anil Mehta")]
    assert learn_pattern("acme.com", mehta, [email("mehta@acme.com")]) is None
    duo = [person("Anil Mehta"), person("Rohit Shah")]
    learned = learn_pattern("acme.com", duo, [email("anil@acme.com"), email("rohit@acme.com")])
    assert learned == LearnedPattern("{first}", 2, ("anil@acme.com", "rohit@acme.com"))


def test_unique_match_required() -> None:
    twins = [person("Priya Nair"), person("Priya Rao")]
    emails = [email("priya@acme.com")]
    assert learn_pattern("acme.com", twins, emails) is None
    for p in twins:
        got = candidates_for(p, domain="acme.com", emails=emails, learned=None, people=twins)
        assert [c.status for c in got] == ["pattern_guess"]


def test_published_single_token_still_matches_person() -> None:
    mehta = person("Anil Mehta")
    got = candidates_for(
        mehta, domain="acme.com", emails=[email("mehta@acme.com")], learned=None, people=[mehta]
    )
    assert got == [Candidate("mehta@acme.com", "published", "page", "{last}")]


def test_short_local_part_ignored() -> None:
    li = person("Li Wei")
    emails = [email("li@acme.com")]
    assert learn_pattern("acme.com", [li], emails) is None
    got = candidates_for(li, domain="acme.com", emails=emails, learned=None, people=[li])
    assert [c.status for c in got] == ["pattern_guess"]


def test_learn_none_without_matching_person() -> None:
    emails = [email("info@acme.com", "role"), email("rk@acme.com")]
    assert learn_pattern("acme.com", [person("Priya Nair")], emails) is None
    assert learn_pattern("acme.com", [], emails) is None
    assert learn_pattern("acme.com", PEOPLE, []) is None


def test_learn_ignores_role_other_domain_and_freemail() -> None:
    emails = [email("priya.nair@acme.com", "role"), email("priya.nair@other.com")]
    assert learn_pattern("acme.com", PEOPLE, emails) is None
    gmail = [email("priya.nair@gmail.com")]
    assert learn_pattern("gmail.com", PEOPLE, gmail) is None


def test_candidates_published_wins() -> None:
    learned = LearnedPattern("{first}.{last}", 3, ())
    emails = [email("pnair@acme.com")]
    got = candidates_for(
        PEOPLE[0], domain="acme.com", emails=emails, learned=learned, people=PEOPLE
    )
    assert got == [Candidate("pnair@acme.com", "published", "page", "{f}{last}")]


def test_candidates_published_role_does_not_count() -> None:
    emails = [email("priya.nair@acme.com", "role")]
    got = candidates_for(PEOPLE[0], domain="acme.com", emails=emails, learned=None, people=PEOPLE)
    assert [c.status for c in got] == ["pattern_guess"]


def test_candidates_confirmed_vs_guess() -> None:
    learned = LearnedPattern("{f}{last}", 2, ())
    got = candidates_for(PEOPLE[0], domain="acme.com", emails=[], learned=learned, people=PEOPLE)
    assert got == [Candidate("pnair@acme.com", "pattern_confirmed", "pattern", "{f}{last}")]
    got = candidates_for(PEOPLE[0], domain="acme.com", emails=[], learned=None, people=PEOPLE)
    assert got == [Candidate("priya.nair@acme.com", "pattern_guess", "guess", "{first}.{last}")]


def test_guess_first_only_and_nothing() -> None:
    got = candidates_for(
        person("Madonna"), domain="acme.com", emails=[], learned=None, people=[person("Madonna")]
    )
    assert got == [Candidate("madonna@acme.com", "pattern_guess", "guess", "{first}")]
    got = candidates_for(
        person("R. K."), domain="acme.com", emails=[], learned=None, people=[person("R. K.")]
    )
    assert got == []


def test_initials_only_person() -> None:
    p = person("R. K. Sharma")
    assert candidates_for(p, domain="acme.com", emails=[], learned=None, people=[p]) == []
    learned = LearnedPattern("{last}", 1, ())
    got = candidates_for(p, domain="acme.com", emails=[], learned=learned, people=[p])
    assert got == [Candidate("sharma@acme.com", "pattern_confirmed", "pattern", "{last}")]


def test_learned_pattern_unrenderable_falls_back_to_guess_rules() -> None:
    learned = LearnedPattern("{first}.{last}", 2, ())
    got = candidates_for(
        person("Madonna"), domain="acme.com", emails=[], learned=learned, people=[person("Madonna")]
    )
    assert got == [Candidate("madonna@acme.com", "pattern_guess", "guess", "{first}")]


def test_free_mail_domain_never_guessed() -> None:
    learned = LearnedPattern("{first}.{last}", 2, ())
    for lr in (None, learned):
        assert (
            candidates_for(PEOPLE[0], domain="gmail.com", emails=[], learned=lr, people=PEOPLE)
            == []
        )


def test_accents() -> None:
    p = person("José Álvarez")
    got = candidates_for(
        p,
        domain="acme.com",
        emails=[],
        learned=LearnedPattern("{first}.{last}", 1, ()),
        people=[p],
    )
    assert got[0].address == "jose.alvarez@acme.com"


def test_multiword_surname_and_hyphen() -> None:
    assert render_address("{first}{last}", "Ludwig", "Van Der Berg", "acme.com") == (
        "ludwigvanderberg@acme.com"
    )
    got = candidates_for(
        person("Mary-Jane Watson"),
        domain="acme.com",
        emails=[],
        learned=None,
        people=[person("Mary-Jane Watson")],
    )
    assert got[0].address == "maryjane.watson@acme.com"


def test_deterministic_and_never_raises() -> None:
    emails = [email("not-an-email"), email("@"), email("x@")]
    assert learn_pattern("", PEOPLE, emails) is None
    assert (
        candidates_for(person(""), domain="", emails=emails, learned=None, people=[person("")])
        == []
    )


def test_same_person_spelled_twice_is_one_person() -> None:
    twice = [person("Priya Nair"), person("PRIYA NAIR"), person("Priya  Nair")]
    emails = [email("priya.nair@acme.com")]
    learned = learn_pattern("acme.com", twice, emails)
    assert learned is not None
    assert learned.pattern == "{first}.{last}"
    got = candidates_for(twice[0], domain="acme.com", emails=emails, learned=None, people=twice)
    assert [c.status for c in got] == ["published"]


def test_learned_pattern_shared_by_two_people_is_only_a_guess() -> None:
    """Amit Shah and Anil Shah both render ashah@ under {f}{last}: a guess for either."""
    shahs = [person("Amit Shah"), person("Anil Shah"), *PEOPLE]
    learned = LearnedPattern("{f}{last}", 2, ("anair@acme.com", "pnair@acme.com"))
    for p in shahs[:2]:
        got = candidates_for(p, domain="acme.com", emails=[], learned=learned, people=shahs)
        assert got == [Candidate("ashah@acme.com", "pattern_guess", "guess", "{f}{last}")]
    got = candidates_for(PEOPLE[0], domain="acme.com", emails=[], learned=learned, people=shahs)
    assert got == [Candidate("pnair@acme.com", "pattern_confirmed", "pattern", "{f}{last}")]
    # the same person listed twice is no clash
    twice = [person("Amit Shah"), person("Mr. Amit Shah")]
    got = candidates_for(twice[0], domain="acme.com", emails=[], learned=learned, people=twice)
    assert got[0].status == "pattern_confirmed"
