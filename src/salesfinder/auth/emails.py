"""Email normalisation: one canonical form for storage and lookup."""

from email_validator import EmailNotValidError, validate_email

INVALID_EMAIL = "Enter a valid email address."


def normalize_email(raw: str) -> str:
    candidate = raw.strip().lower()
    try:
        result = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(INVALID_EMAIL) from exc
    return result.normalized.lower()
