"""The operator docs stay in step with the code: every ``make <target>`` they mention is a
Makefile target, and every ``salesfinder <cmd> [<sub>] [options]`` in a shell block or inline
code parses as a real command with real options.
Plans and specs under docs/superpowers/ are history and aren't checked."""

import re
import shlex
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup

from salesfinder.cli import app

REPO = Path(__file__).resolve().parent.parent
SHELL_FENCES = frozenset({"sh", "bash", "shell", "zsh", "console"})
_FENCE_RE = re.compile(r"^```(\S*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MAKE_RE = re.compile(r"^(?:\$\s+)?make\s+([a-z][a-z0-9_-]*)")
_TARGET_RE = re.compile(r"^([a-z][a-z0-9_-]*)\s*:(?!=)", re.MULTILINE)
# ``salesfinder`` at a command's start: the line's start, after ``uv run`` or a ``…/bin/`` path,
# or after a shell separator. ``createdb salesfinder`` or ``pg_dump … salesfinder`` don't match.
# The arguments run up to a shell separator, a redirect or a comment.
_CLI_RE = re.compile(
    r"(?:^\s*(?:\$\s+)?|uv run\s+|/bin/|[;&|]\s*)salesfinder\s+(?=[a-z])([^;&|>#\n]*)",
    re.MULTILINE,
)
# A ``<placeholder>`` stands for one value.
_PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")


def _docs() -> list[Path]:
    return [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]


def _blocks(text: str) -> tuple[list[str], list[str]]:
    """(lines inside shell code fences, prose lines outside any fence)."""
    shell: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line.strip())
        if match is not None:
            fence = None if fence is not None else match.group(1).lower()
            continue
        if fence is None:
            prose.append(line)
        elif fence in SHELL_FENCES:
            shell.append(line)
    return shell, prose


def _make_targets_mentioned() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for doc in _docs():
        shell, prose = _blocks(doc.read_text(encoding="utf-8"))
        candidates = [line.strip() for line in shell]
        candidates += [span.strip() for line in prose for span in _INLINE_CODE_RE.findall(line)]
        for candidate in candidates:
            for part in re.split(r"\s*(?:&&|;|\|\|)\s*", candidate):
                match = _MAKE_RE.match(part)
                if match is not None:
                    found.setdefault(match.group(1), set()).add(doc.name)
    return found


def _makefile_targets() -> set[str]:
    return set(_TARGET_RE.findall((REPO / "Makefile").read_text(encoding="utf-8")))


def _cli_commands_mentioned() -> list[tuple[str, list[str], bool]]:
    """(doc, argv after ``salesfinder``, whether it is inline code in prose) for every command
    in a shell block or an inline code span; placeholders become a plain value."""
    found: list[tuple[str, list[str], bool]] = []
    for doc in _docs():
        shell, prose = _blocks(doc.read_text(encoding="utf-8"))
        codes = [(line.split(" #", 1)[0], False) for line in shell]  # comments aren't code
        codes += [(span, True) for line in prose for span in _INLINE_CODE_RE.findall(line)]
        for code, inline in codes:
            for match in _CLI_RE.finditer(_PLACEHOLDER_RE.sub("x", code)):
                found.append((doc.name, shlex.split(match.group(1)), inline))
    return found


def _problem(argv: list[str], *, name_only_ok: bool = False) -> str | None:
    """Why ``salesfinder <argv>`` wouldn't parse (unknown command or subcommand, unknown
    option, missing value...), or None. Parses only: nothing runs. With ``name_only_ok`` a
    bare command name (prose naming ``salesfinder draft``) needn't carry its required
    options."""
    root = _command_tree()
    name, *rest = argv
    command = root.commands.get(name)
    if command is None:
        return "unknown command"
    if isinstance(command, TyperGroup):
        if not rest or rest[0] not in command.commands:
            return "unknown or missing subcommand"
        name, command, rest = rest[0], command.commands[rest[0]], rest[1:]
    if name_only_ok and not rest:
        return None
    try:
        command.make_context(name, list(rest))
    except Exception as exc:  # click's UsageError and friends
        return f"{type(exc).__name__}: {exc}"
    return None


def test_docs_exist_and_are_scanned() -> None:
    names = {doc.name for doc in _docs()}
    assert {"README.md", "operations.md", "architecture.md"} <= names


def test_every_make_target_in_the_docs_exists() -> None:
    mentioned = _make_targets_mentioned()
    # Not vacuous: the operator docs lean on these.
    assert {"setup", "migrate", "plists", "start", "stop", "restart", "logs"} <= set(mentioned)
    missing = {t: sorted(docs) for t, docs in mentioned.items() if t not in _makefile_targets()}
    assert missing == {}


def test_ops_targets_are_phony() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^\.PHONY:(.*)$", makefile, re.MULTILINE)
    assert match is not None
    phony = set(match.group(1).split())
    assert {"plists", "start", "stop", "restart", "logs", "e2e"} <= phony
    assert _makefile_targets() <= phony


def _command_tree() -> TyperGroup:
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    return root


def test_every_salesfinder_command_in_the_docs_parses() -> None:
    mentioned = _cli_commands_mentioned()
    assert len(mentioned) >= 30  # not vacuous: shell blocks and inline code both count
    argvs = [argv for _, argv, _ in mentioned]
    assert ["ops", "plists", "--out", "~/Library/LaunchAgents", "--force"] in argvs
    assert ["worker", "--no-send"] in argvs
    assert any(inline for _, _, inline in mentioned)
    problems = [
        f"{doc}: salesfinder {' '.join(argv)} — {problem}"
        for doc, argv, inline in mentioned
        if (problem := _problem(argv, name_only_ok=inline)) is not None
    ]
    assert problems == []


@pytest.mark.parametrize(
    "line",
    [
        "uv run salesfinder nosuch",
        "salesfinder sending nosuch",
        ".venv/bin/salesfinder ops",
        "uv run salesfinder worker --no-such-flag",
        "salesfinder sending pause --reason",
        "salesfinder runs --limit lots",
    ],
)
def test_the_command_check_catches_mistakes(line: str) -> None:
    match = _CLI_RE.search(_PLACEHOLDER_RE.sub("x", line))
    assert match is not None
    assert _problem(shlex.split(match.group(1))) is not None


def test_the_command_check_accepts_placeholders_and_quotes() -> None:
    line = 'uv run salesfinder users create-admin --email <you> --name "A B"  # prompts'
    match = _CLI_RE.search(_PLACEHOLDER_RE.sub("x", line.split(" #", 1)[0]))
    assert match is not None
    argv = shlex.split(match.group(1))
    assert argv == ["users", "create-admin", "--email", "x", "--name", "A B"]
    assert _problem(argv) is None


def test_operations_doc_states_the_rules() -> None:
    text = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
    assert "Restart the web app and the worker together" in text
    assert "manual step the operator runs" in text


def test_readme_links_the_docs() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "(docs/operations.md)" in text
    assert "(docs/architecture.md)" in text


# The README's documented example app passwords (obviously not real).
EXAMPLE_APP_PASSWORDS = frozenset({"abcd efgh ijkl mnop", "qrst uvwx yzab cdef"})


def test_docs_hold_no_real_secrets() -> None:
    """Placeholders only: no quoted app-password-shaped value but the README's two documented
    examples, no private key, no HubSpot token, no secret assigned a value."""
    for doc in _docs():
        text = doc.read_text(encoding="utf-8")
        assert not re.search(r"BEGIN [A-Z ]*PRIVATE KEY", text), doc.name
        assert not re.search(r"pat-[a-z0-9]{2}-[0-9a-f-]{20,}", text), doc.name  # HubSpot token
        passwords = set(re.findall(r"\"([a-z]{4}(?: [a-z]{4}){3})\"", text))
        assert passwords <= EXAMPLE_APP_PASSWORDS, (doc.name, passwords)
        for name, value in re.findall(
            r"SALESFINDER_(SECRET_KEY|HUBSPOT_TOKEN|SERPER_API_KEY|EMAIL_VERIFIER_KEY)=(\S*)", text
        ):
            assert value == "" or _PLACEHOLDER_RE.fullmatch(value), (doc.name, name)
