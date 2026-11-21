"""Claude CLI flags SalesFinder depends on."""

REQUIRED_FLAGS: tuple[str, ...] = (
    "--safe-mode",
    "--setting-sources",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--tools",
    "--output-format",
)


def missing_flags(help_text: str) -> list[str]:
    return [flag for flag in REQUIRED_FLAGS if flag not in help_text]
