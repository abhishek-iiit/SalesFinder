"""YAML application config: salesfinder.yaml deep-merged with salesfinder.local.yaml."""

import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from salesfinder.industries import INDUSTRIES
from salesfinder.signals.catalog import for_side

BASE_FILE = "salesfinder.yaml"
LOCAL_FILE = "salesfinder.local.yaml"


class ConfigError(Exception):
    """Raised when the YAML config is missing, malformed, or invalid."""


def _reject_unquoted_time(v: object) -> object:
    # Unquoted 22:00 is a YAML 1.1 base-60 integer; pydantic would read it as seconds.
    if isinstance(v, int) and not isinstance(v, bool):
        raise ValueError('use a quoted "HH:MM" string (unquoted HH:MM is parsed as a number)')
    return v


def _reject_offset_time(v: time, zone: str) -> time:
    # "22:00Z" or "22:00+05:30" parses to an offset-aware time, which can't be compared with
    # the naive wall-clock times the checks use (TypeError at runtime).
    if v.tzinfo is not None:
        raise ValueError(f"give a wall-clock HH:MM; the zone is {zone}")
    return v


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: time = time(22, 0)
    end: time = time(7, 0)
    timezone: str = "America/New_York"

    @field_validator("start", "end", mode="before")
    @classmethod
    def _times_must_be_quoted(cls, v: object) -> object:
        return _reject_unquoted_time(v)

    @field_validator("start", "end")
    @classmethod
    def _wall_clock(cls, v: time) -> time:
        return _reject_offset_time(v, "quiet_hours.timezone")

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class UsageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Minutes of worker passes one ``runs`` row covers (the heartbeat log's window).
    heartbeat_interval_minutes: int = Field(15, ge=1, le=1440)
    max_daily_claude_calls: int = 150
    quiet_hours: QuietHours = QuietHours()


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    country: str
    gl: str


def _default_templates() -> dict[str, str]:
    return {
        "buyer": '"{chemical}" {keyword} manufacturer {region}',
        "supplier": '"{chemical}" manufacturer exporter {region}',
    }


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A zero or negative price would make every search "free" and disable the budget stop.
    serper_price_per_1000_usd: Decimal = Field(default=Decimal("1.00"), gt=0)
    monthly_budget_usd: Decimal = Field(default=Decimal("20.00"), ge=0)
    pages_per_query: int = Field(default=1, ge=1, le=3)
    results_per_page: int = 10
    max_queries_per_job: int = Field(default=200, ge=1)
    templates: dict[Literal["buyer", "supplier"], str] = Field(default_factory=_default_templates)  # type: ignore[arg-type]
    industry_keywords: dict[str, list[str]] = {}
    buyer_regions: list[Region] = []
    supplier_regions: list[Region] = []
    junk_domains: list[str] = []

    @field_validator("serper_price_per_1000_usd", "monthly_budget_usd", mode="before")
    @classmethod
    def _decimal_from_text(cls, v: object) -> object:
        # YAML floats carry binary artifacts; go through the decimal text instead.
        if isinstance(v, float):
            return Decimal(str(v))
        return v

    @field_validator("results_per_page")
    @classmethod
    def _fixed_page_size(cls, v: int) -> int:
        if v != 10:
            raise ValueError("results_per_page is fixed at 10")
        return v

    @field_validator("templates")
    @classmethod
    def _templates_have_placeholders(cls, v: dict[str, str]) -> dict[str, str]:
        required = {
            "buyer": ("{chemical}", "{keyword}", "{region}"),
            "supplier": ("{chemical}", "{region}"),
        }
        for side, placeholders in required.items():
            template = v.get(side)
            if template is None:
                raise ValueError(f"templates.{side} is required")
            missing = [p for p in placeholders if p not in template]
            if missing:
                raise ValueError(f"templates.{side} must contain {', '.join(missing)}")
        return v

    @field_validator("industry_keywords")
    @classmethod
    def _known_industries(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = sorted(set(v) - set(INDUSTRIES))
        if unknown:
            raise ValueError(f"unknown industry key(s): {', '.join(unknown)}")
        return v


class HubSpotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_interval_hours: int = Field(default=24, ge=1)
    stale_after_hours: int = Field(default=48, ge=1)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_seconds: int = Field(default=10, ge=1)


class ProfilingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=10, ge=1)
    concurrency: int = Field(default=4, ge=1, le=16)
    max_pages_per_company: int = Field(default=8, ge=1, le=20)
    page_timeout_seconds: float = Field(default=10, gt=0)
    max_page_bytes: int = Field(default=2_000_000, ge=10_000)
    refresh_after_days: int = Field(default=30, ge=1)
    user_agent: str = "SalesFinderBot/0.1 (+https://elchemy.com)"


class ContactsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_targets_per_company: int = Field(default=2, ge=1, le=5)
    dns_timeout_seconds: float = Field(default=5, gt=0)
    dns_concurrency: int = Field(default=8, ge=1, le=32)
    mx_cache_days: int = Field(default=7, ge=1)
    batch_size: int = Field(default=10, ge=1)
    recheck_after_days: int = Field(default=30, ge=1)
    verifier: Literal["none", "millionverifier"] = "none"
    verifier_max_per_day: int = Field(default=100, ge=0)


def _default_buyer_weights() -> dict[str, int]:
    return {
        "USES_CATALOG_CHEMICAL": 30,
        "INDUSTRY_MATCH": 15,
        "COMPANY_TYPE_PRODUCER": 20,
        "COMPANY_TYPE_DISTRIBUTOR": 10,
        "US_LOCATION": 5,
        "NEAR_WAREHOUSE": 10,
        "HIRING_PROCUREMENT": 10,
        "QUALITY_CERTS": 10,
    }


def _default_supplier_weights() -> dict[str, int]:
    return {
        "MAKES_CATALOG_CHEMICAL": 35,
        "MANUFACTURER": 20,
        "EXPORTS": 10,
        "EXPORTS_TO_US": 10,
        "SUPPLIER_CERTS": 15,
        "STATED_CAPACITY": 10,
    }


# Weight keys beyond the signal codes of a side: one signal can earn different points by value.
_PSEUDO_WEIGHT_KEYS = {
    "buyer": {"COMPANY_TYPE_PRODUCER", "COMPANY_TYPE_DISTRIBUTOR"},
    "supplier": {"MANUFACTURER"},
}


_DEFAULT_WEIGHTS = {"buyer": _default_buyer_weights, "supplier": _default_supplier_weights}


def _check_weights(side: Literal["buyer", "supplier"], weights: dict[str, int]) -> dict[str, int]:
    """The given weights merged onto the side's defaults (a key left out keeps its default);
    an unknown key or a weight outside 0-100 is an error."""
    allowed = {s.code for s in for_side(side)} | _PSEUDO_WEIGHT_KEYS[side]
    for key, value in weights.items():
        if key not in allowed:
            raise ValueError(f"unknown {side} scoring key {key!r}")
        if not 0 <= value <= 100:
            raise ValueError(f"{side} scoring weight {key} must be 0-100, got {value}")
    return {**_DEFAULT_WEIGHTS[side](), **weights}


class ScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    near_warehouse_miles: int = Field(default=500, ge=1)
    buyer: dict[str, int] = Field(default_factory=_default_buyer_weights)
    supplier: dict[str, int] = Field(default_factory=_default_supplier_weights)

    @field_validator("buyer")
    @classmethod
    def _buyer_keys(cls, v: dict[str, int]) -> dict[str, int]:
        return _check_weights("buyer", v)

    @field_validator("supplier")
    @classmethod
    def _supplier_keys(cls, v: dict[str, int]) -> dict[str, int]:
        return _check_weights("supplier", v)


_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Seconds launchd waits after SIGTERM before it SIGKILLs the worker (its plist's ExitTimeOut).
WORKER_EXIT_TIMEOUT_SECONDS = 600
# A stopping worker's pass can still wait on one reply's Claude calls (at most this many, each
# up to brain.timeout_seconds; Ruling P7-G) ...
STOPPING_CLAUDE_CALLS = 4
# ... plus the rest of the pass (sending, mail, the runs writes).
STOP_MARGIN_SECONDS = 60
# So brain.timeout_seconds must stay under this (135 s): a planned stop is never SIGKILLed.
MAX_BRAIN_TIMEOUT_SECONDS = (
    WORKER_EXIT_TIMEOUT_SECONDS - STOP_MARGIN_SECONDS
) / STOPPING_CLAUDE_CALLS


class BrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty means the CLI's default model.
    model: str = ""
    # Per call; under MAX_BRAIN_TIMEOUT_SECONDS so a stopping worker ends within ExitTimeOut.
    timeout_seconds: float = Field(default=120, ge=10, lt=MAX_BRAIN_TIMEOUT_SECONDS)

    @field_validator("model")
    @classmethod
    def _model_name(cls, v: str) -> str:
        if v and not _MODEL_RE.fullmatch(v):
            raise ValueError("model must be 1-64 characters of letters, digits, '.', '_' or '-'")
        return v


DEFAULT_OPT_OUT_LINE = (
    "If you'd rather not hear from us, just reply \"unsubscribe\" and we won't email you again."
)


def _default_banned_phrases() -> list[str]:
    return [
        "guarantee",
        "guarantees",
        "guaranteed",
        "lowest price",
        "cheapest",
        "best price",
        "risk-free",
        "act now",
        "limited time",
        "100%",
        "no obligation",
        "free trial",
    ]


class WritingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_email_max_words: int = Field(default=140, ge=20, le=500)
    followup_max_words: int = Field(default=100, ge=20, le=500)
    subject_max_chars: int = Field(default=80, ge=10, le=200)
    followup_business_days: int = Field(default=4, ge=1, le=30)
    # Empty, or an https link the writer may include.
    booking_url: str = ""
    opt_out_line: str = Field(default=DEFAULT_OPT_OUT_LINE, min_length=1, max_length=300)
    banned_phrases: list[str] = Field(default_factory=_default_banned_phrases)
    max_feedback_items: int = Field(default=5, ge=1, le=20)
    reply_max_words: int = Field(default=180, ge=20, le=500)
    # Most seconds one writing pass (follow-ups and replies) may run.
    max_pass_seconds: int = Field(default=300, ge=30, le=3600)
    # The worker's writing step runs at most once every this many minutes.
    min_interval_minutes: int = Field(default=5, ge=1, le=1440)
    # Most companies one drafting pass (worker or `salesfinder draft`) asks Claude about.
    batch_size: int = Field(default=10, ge=1, le=100)

    @field_validator("booking_url")
    @classmethod
    def _https_or_empty(cls, v: str) -> str:
        if v and not (v.startswith("https://") and len(v) > len("https://") and " " not in v):
            raise ValueError("booking_url must be empty or an https:// URL")
        return v

    @field_validator("banned_phrases")
    @classmethod
    def _clean_phrases(cls, v: list[str]) -> list[str]:
        # An empty phrase would match every draft; matching is case-insensitive.
        cleaned: list[str] = []
        for phrase in v:
            text = phrase.strip().lower()
            if not text:
                raise ValueError("banned phrases must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class SendingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Recipient-local office hours (weekdays only).
    office_start: time = time(9, 0)
    office_end: time = time(17, 0)
    min_spacing_seconds: int = Field(default=180, ge=0)
    max_spacing_seconds: int = Field(default=540, ge=0)
    # No cold email for this many days after a mailbox's warm-up start.
    warmup_days: int = Field(default=14, ge=0)
    ramp_start: int = Field(default=5, ge=1)
    ramp_step: int = Field(default=3, ge=0)
    max_daily_cap: int = Field(default=50, ge=1, le=50)
    # Pause all sending if the bounce rate over the last bounce_window sends exceeds this.
    bounce_window: int = Field(default=50, ge=1)
    bounce_max_rate: float = Field(default=0.05, ge=0, le=1)
    reconcile_after_minutes: int = Field(default=10, ge=1)
    max_send_attempts: int = Field(default=3, ge=1)
    retry_minutes: list[int] = Field(default_factory=lambda: [5, 15])
    dns_max_age_hours: int = Field(default=24, ge=1)
    smtp_timeout_seconds: float = Field(default=30, gt=0)
    candidates_per_pass: int = Field(default=20, ge=1)

    @field_validator("office_start", "office_end", mode="before")
    @classmethod
    def _times_must_be_quoted(cls, v: object) -> object:
        return _reject_unquoted_time(v)

    @field_validator("office_start", "office_end")
    @classmethod
    def _wall_clock(cls, v: time) -> time:
        return _reject_offset_time(v, "the recipient's local time")

    @field_validator("retry_minutes")
    @classmethod
    def _retry_positive(cls, v: list[int]) -> list[int]:
        if any(m < 1 for m in v):
            raise ValueError("retry_minutes must all be at least 1")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_spacing_seconds > self.max_spacing_seconds:
            raise ValueError("min_spacing_seconds must not exceed max_spacing_seconds")
        if self.office_start >= self.office_end:
            raise ValueError("office_start must be before office_end")
        return self


class MailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_minutes: int = Field(default=5, ge=1)
    imap_timeout_seconds: float = Field(default=30, gt=0)
    max_messages_per_poll: int = Field(default=200, ge=1)
    max_message_bytes: int = Field(default=5_000_000, ge=1000)
    max_body_chars: int = Field(default=20_000, ge=100)


class RepliesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(default=0.7, ge=0, le=1)
    max_classify_failures: int = Field(default=2, ge=1)
    batch_size: int = Field(default=10, ge=1)
    max_pass_seconds: int = Field(default=300, ge=30)


class OpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How often the worker's background beat refreshes its ``runs`` row while it is alive.
    beat_seconds: int = Field(default=30, ge=5, le=300)
    # The worker counts as stopped responding after this long without a beat.
    worker_stale_seconds: int = Field(default=180, le=86400)
    # Finished ``runs`` rows older than this are pruned.
    runs_keep_days: int = Field(default=30, ge=1, le=365)
    # Most lines one ``runs`` row keeps; the rest are counted, not kept.
    max_run_lines: int = Field(default=200, ge=10, le=2000)

    @model_validator(mode="after")
    def _stale_after_two_beats(self) -> Self:
        # One missed beat (a slow database write) must never read as "stopped responding".
        if self.worker_stale_seconds < 2 * self.beat_seconds:
            raise ValueError("worker_stale_seconds must be at least twice beat_seconds")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: UsageConfig = UsageConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    hubspot: HubSpotConfig = HubSpotConfig()
    worker: WorkerConfig = WorkerConfig()
    profiling: ProfilingConfig = ProfilingConfig()
    scoring: ScoringConfig = ScoringConfig()
    contacts: ContactsConfig = ContactsConfig()
    brain: BrainConfig = BrainConfig()
    writing: WritingConfig = WritingConfig()
    sending: SendingConfig = SendingConfig()
    mail: MailConfig = MailConfig()
    replies: RepliesConfig = RepliesConfig()
    ops: OpsConfig = OpsConfig()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge dicts; any non-dict value (including lists) is replaced."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def load_config(config_dir: Path) -> AppConfig:
    base_path = config_dir / BASE_FILE
    local_path = config_dir / LOCAL_FILE
    if not base_path.is_file():
        raise ConfigError(f"{base_path}: required config file {BASE_FILE} not found")
    data = _read_yaml(base_path)
    sources = BASE_FILE
    if local_path.is_file():
        data = deep_merge(data, _read_yaml(local_path))
        sources = f"{BASE_FILE} (+ {LOCAL_FILE})"
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']} (value: {err['input']!r})"
            for err in exc.errors()
        )
        raise ConfigError(f"{config_dir / sources}: invalid config: {problems}") from exc
