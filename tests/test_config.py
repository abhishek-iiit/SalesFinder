from decimal import Decimal
from pathlib import Path

import pytest

from salesfinder.config import AppConfig, ConfigError, deep_merge, load_config


def test_deep_merge_recurses_dicts_and_replaces_lists():
    assert deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}}) == {"a": {"b": 1, "c": [2]}}


def test_load_defaults(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  max_daily_claude_calls: 150\n")
    cfg = load_config(tmp_path)
    assert cfg.usage.max_daily_claude_calls == 150
    assert cfg.usage.heartbeat_interval_minutes == 15


def test_local_overrides(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  max_daily_claude_calls: 150\n")
    (tmp_path / "salesfinder.local.yaml").write_text("usage:\n  max_daily_claude_calls: 60\n")
    assert load_config(tmp_path).usage.max_daily_claude_calls == 60


def test_unknown_key_names_file_and_key(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  bogus: 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(tmp_path)


def test_bad_timezone(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  quiet_hours:\n    timezone: Mars/Base\n")
    with pytest.raises(ConfigError, match="Mars/Base"):
        load_config(tmp_path)


def test_missing_base_file(tmp_path):
    with pytest.raises(ConfigError, match=r"salesfinder\.yaml"):
        load_config(tmp_path)


def test_invalid_yaml_names_file(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage: [unclosed\n")
    with pytest.raises(ConfigError, match=r"salesfinder\.yaml"):
        load_config(tmp_path)


def test_shipped_config_loads(shipped_config_dir: Path):
    # A temp copy of the shipped files, never the repo dir (a developer's .local.yaml).
    cfg = load_config(shipped_config_dir)
    assert cfg.usage.quiet_hours.timezone == "America/New_York"
    assert cfg.usage.quiet_hours.start.hour == 22


def test_shipped_discovery_config(shipped_config: AppConfig):
    cfg = shipped_config
    assert len(cfg.discovery.buyer_regions) == 8
    assert len(cfg.discovery.supplier_regions) == 8
    assert cfg.discovery.serper_price_per_1000_usd == Decimal("1")
    assert cfg.hubspot.sync_interval_hours == 24 and cfg.worker.poll_seconds == 10


def test_decimal_from_yaml_float(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("discovery:\n  serper_price_per_1000_usd: 1.00\n")
    cfg = load_config(tmp_path)
    assert cfg.discovery.serper_price_per_1000_usd == Decimal("1.00")
    assert isinstance(cfg.discovery.serper_price_per_1000_usd, Decimal)


def test_buyer_template_missing_keyword(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text(
        "discovery:\n  templates:\n    buyer: '\"{chemical}\" {region}'\n"
    )
    with pytest.raises(ConfigError, match="templates"):
        load_config(tmp_path)


def test_pages_per_query_out_of_range(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("discovery:\n  pages_per_query: 4\n")
    with pytest.raises(ConfigError, match="pages_per_query"):
        load_config(tmp_path)


def test_unknown_industry_keyword_key(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text(
        "discovery:\n  industry_keywords:\n    plastics: [resin]\n"
    )
    with pytest.raises(ConfigError, match="plastics"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("yaml_text", "key"),
    [
        ("discovery:\n  serper_price_per_1000_usd: 0\n", "discovery.serper_price_per_1000_usd"),
        ("discovery:\n  serper_price_per_1000_usd: -1\n", "discovery.serper_price_per_1000_usd"),
        ("discovery:\n  monthly_budget_usd: -0.01\n", "discovery.monthly_budget_usd"),
        ("discovery:\n  max_queries_per_job: 0\n", "discovery.max_queries_per_job"),
        ("worker:\n  poll_seconds: 0\n", "worker.poll_seconds"),
        ("hubspot:\n  sync_interval_hours: 0\n", "hubspot.sync_interval_hours"),
        ("hubspot:\n  stale_after_hours: 0\n", "hubspot.stale_after_hours"),
        ("profiling:\n  concurrency: 0\n", "profiling.concurrency"),
        ("profiling:\n  concurrency: 17\n", "profiling.concurrency"),
        ("profiling:\n  max_page_bytes: 100\n", "profiling.max_page_bytes"),
        ("scoring:\n  buyer:\n    INDUSTRY_MATCH: 101\n", "scoring.buyer"),
    ],
)
def test_out_of_bounds_numbers_are_rejected(tmp_path: Path, yaml_text: str, key: str) -> None:
    # A price of 0 (or less) would make every search free and disable the budget hard stop.
    (tmp_path / "salesfinder.yaml").write_text(yaml_text)
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert key in str(info.value)


def test_zero_budget_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("discovery:\n  monthly_budget_usd: 0\n")
    assert load_config(tmp_path).discovery.monthly_budget_usd == Decimal(0)


def test_shipped_scoring_and_profiling_defaults(shipped_config_dir: Path) -> None:
    cfg = load_config(shipped_config_dir)
    assert cfg.scoring.near_warehouse_miles == 500
    assert cfg.scoring.buyer == {
        "USES_CATALOG_CHEMICAL": 30,
        "INDUSTRY_MATCH": 15,
        "COMPANY_TYPE_PRODUCER": 20,
        "COMPANY_TYPE_DISTRIBUTOR": 10,
        "US_LOCATION": 5,
        "NEAR_WAREHOUSE": 10,
        "HIRING_PROCUREMENT": 10,
        "QUALITY_CERTS": 10,
    }
    assert cfg.scoring.supplier["MAKES_CATALOG_CHEMICAL"] == 35
    assert cfg.profiling.concurrency == 4 and cfg.profiling.max_pages_per_company == 8
    assert cfg.profiling.user_agent.startswith("SalesFinderBot/")


def test_unknown_scoring_key_names_it(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("scoring:\n  buyer:\n    EXPORTS: 5\n")
    with pytest.raises(ConfigError, match="EXPORTS"):
        load_config(tmp_path)
    (tmp_path / "salesfinder.yaml").write_text("scoring:\n  supplier:\n    NOPE: 5\n")
    with pytest.raises(ConfigError, match="NOPE"):
        load_config(tmp_path)


def test_partial_scoring_weights_keep_defaults(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("scoring:\n  buyer:\n    QUALITY_CERTS: 0\n")
    cfg = load_config(tmp_path)
    assert cfg.scoring.buyer["QUALITY_CERTS"] == 0
    assert cfg.scoring.buyer["USES_CATALOG_CHEMICAL"] == 30
    assert set(cfg.scoring.buyer) == set(AppConfig().scoring.buyer)
    assert cfg.scoring.supplier == AppConfig().scoring.supplier


def test_contacts_defaults_and_shipped_yaml(shipped_config: AppConfig) -> None:
    expected = {
        "max_targets_per_company": 2,
        "dns_timeout_seconds": 5,
        "dns_concurrency": 8,
        "mx_cache_days": 7,
        "batch_size": 10,
        "recheck_after_days": 30,
        "verifier": "none",
        "verifier_max_per_day": 100,
    }
    assert AppConfig().contacts.model_dump() == expected
    assert shipped_config.contacts.model_dump() == expected
    assert AppConfig().profiling.max_pages_per_company == 8


@pytest.mark.parametrize(
    "body",
    [
        "verifier: zerobounce",
        "dns_concurrency: 0",
        "dns_concurrency: 33",
        "max_targets_per_company: 0",
        "max_targets_per_company: 6",
        "dns_timeout_seconds: 0",
        "mx_cache_days: 0",
        "batch_size: 0",
        "recheck_after_days: 0",
        "verifier_max_per_day: -1",
    ],
)
def test_contacts_bounds(tmp_path: Path, body: str) -> None:
    (tmp_path / "salesfinder.yaml").write_text(f"contacts:\n  {body}\n")
    with pytest.raises(ConfigError, match="contacts"):
        load_config(tmp_path)


_OPT_OUT = (
    "If you'd rather not hear from us, just reply \"unsubscribe\" and we won't email you again."
)
_BANNED = [
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


def test_brain_and_writing_defaults_and_shipped_yaml(shipped_config: AppConfig) -> None:
    writing = {
        "first_email_max_words": 140,
        "followup_max_words": 100,
        "subject_max_chars": 80,
        "followup_business_days": 4,
        "booking_url": "",
        "opt_out_line": _OPT_OUT,
        "banned_phrases": _BANNED,
        "max_feedback_items": 5,
        "min_interval_minutes": 5,
        "batch_size": 10,
        "reply_max_words": 180,
        "max_pass_seconds": 300,
    }
    brain = {"model": "", "timeout_seconds": 120}
    assert AppConfig().writing.model_dump() == writing
    assert AppConfig().brain.model_dump() == brain
    shipped = shipped_config
    assert shipped.writing.model_dump() == writing
    assert shipped.brain.model_dump() == brain


@pytest.mark.parametrize(
    "body",
    [
        "brain:\n  model: 'bad model'",
        "brain:\n  model: 'a/b'",
        "brain:\n  model: " + "'" + "a" * 65 + "'",
        "brain:\n  timeout_seconds: 9",
        "brain:\n  timeout_seconds: 601",
        "brain:\n  timeout_seconds: 135",  # 4 x 135 + 60 s reaches the 600 s ExitTimeOut
        "writing:\n  booking_url: 'http://x.com/m'",
        "writing:\n  booking_url: 'x.com'",
        "writing:\n  first_email_max_words: 0",
        "writing:\n  followup_max_words: 0",
        "writing:\n  subject_max_chars: 0",
        "writing:\n  followup_business_days: 0",
        "writing:\n  max_feedback_items: 0",
        "writing:\n  min_interval_minutes: 0",
        "writing:\n  min_interval_minutes: 1441",
        "writing:\n  batch_size: 0",
        "writing:\n  batch_size: 101",
        "writing:\n  opt_out_line: ''",
        "writing:\n  bogus: 1",
    ],
)
def test_brain_writing_bounds(tmp_path: Path, body: str) -> None:
    (tmp_path / "salesfinder.yaml").write_text(body + "\n")
    with pytest.raises(ConfigError, match=r"brain|writing"):
        load_config(tmp_path)


def test_brain_writing_valid_values(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text(
        "brain:\n  model: claude-sonnet-4.5\n  timeout_seconds: 10\n"
        "writing:\n  booking_url: https://cal.example.com/elchemy\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.brain.model == "claude-sonnet-4.5" and cfg.brain.timeout_seconds == 10
    assert cfg.writing.booking_url == "https://cal.example.com/elchemy"


@pytest.mark.parametrize("phrases", ['[""]', '["   "]', '["guarantee", " "]', '["ok", "\\t"]'])
def test_banned_phrases_reject_empty(tmp_path: Path, phrases: str) -> None:
    (tmp_path / "salesfinder.yaml").write_text(f"writing:\n  banned_phrases: {phrases}\n")
    with pytest.raises(ConfigError, match=r"writing\.banned_phrases"):
        load_config(tmp_path)


def test_banned_phrases_normalized(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text(
        'writing:\n  banned_phrases: ["Guarantee", "guarantee ", "  Lowest Price", "x"]\n'
    )
    assert load_config(tmp_path).writing.banned_phrases == ["guarantee", "lowest price", "x"]


def test_banned_phrases_may_be_empty_list(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("writing:\n  banned_phrases: []\n")
    assert load_config(tmp_path).writing.banned_phrases == []


def test_sending_mail_replies_defaults(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("{}\n")
    cfg = load_config(tmp_path)
    s = cfg.sending
    assert (s.office_start.hour, s.office_end.hour) == (9, 17)
    assert (s.min_spacing_seconds, s.max_spacing_seconds) == (180, 540)
    assert (s.warmup_days, s.ramp_start, s.ramp_step, s.max_daily_cap) == (14, 5, 3, 50)
    assert (s.bounce_window, s.bounce_max_rate, s.reconcile_after_minutes) == (50, 0.05, 10)
    assert (s.max_send_attempts, s.retry_minutes, s.dns_max_age_hours) == (3, [5, 15], 24)
    assert (s.smtp_timeout_seconds, s.candidates_per_pass) == (30, 20)
    m = cfg.mail
    assert (m.poll_interval_minutes, m.imap_timeout_seconds, m.max_messages_per_poll) == (
        5,
        30,
        200,
    )
    assert (m.max_message_bytes, m.max_body_chars) == (5_000_000, 20_000)
    r = cfg.replies
    assert (r.min_confidence, r.max_classify_failures, r.batch_size, r.max_pass_seconds) == (
        0.7,
        2,
        10,
        300,
    )
    assert (cfg.writing.reply_max_words, cfg.writing.max_pass_seconds) == (180, 300)


def test_shipped_sending_config_matches_defaults(shipped_config_dir: Path) -> None:
    cfg = load_config(shipped_config_dir)
    assert cfg.sending == AppConfig().sending
    assert cfg.mail == AppConfig().mail
    assert cfg.replies == AppConfig().replies


@pytest.mark.parametrize(
    "yaml_text",
    [
        "sending:\n  min_spacing_seconds: 600\n  max_spacing_seconds: 100\n",
        "sending:\n  office_start: 9:00\n",
        "sending:\n  office_end: 17:00\n",
        'sending:\n  office_start: "18:00"\n  office_end: "09:00"\n',
        "sending:\n  max_daily_cap: 51\n",
        "sending:\n  retry_minutes: [0]\n",
        "writing:\n  reply_max_words: 19\n",
        "writing:\n  reply_max_words: 501\n",
        "writing:\n  max_pass_seconds: 29\n",
        "writing:\n  max_pass_seconds: 3601\n",
        "replies:\n  min_confidence: 1.5\n",
        "sending:\n  surprise: 1\n",
    ],
)
def test_sending_config_bounds(tmp_path: Path, yaml_text: str) -> None:
    (tmp_path / "salesfinder.yaml").write_text(yaml_text)
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_sending_quoted_office_hours_ok(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text(
        'sending:\n  office_start: "08:30"\n  office_end: "18:00"\n'
    )
    cfg = load_config(tmp_path)
    assert (cfg.sending.office_start.minute, cfg.sending.office_end.hour) == (30, 18)


# --------------------------------------------------------------------------- Phase 7 ops


def test_ops_defaults_and_shipped_yaml(tmp_path: Path, shipped_config_dir: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("{}\n")
    ops = load_config(tmp_path).ops
    assert (ops.beat_seconds, ops.worker_stale_seconds) == (30, 180)
    assert (ops.runs_keep_days, ops.max_run_lines) == (30, 200)
    shipped = load_config(shipped_config_dir)
    assert shipped.ops == AppConfig().ops
    assert shipped.usage.heartbeat_interval_minutes == 15


def test_ops_stale_must_be_twice_the_beat(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text(
        "ops:\n  beat_seconds: 30\n  worker_stale_seconds: 40\n"
    )
    with pytest.raises(ConfigError, match=r"ops.*worker_stale_seconds must be at least twice"):
        load_config(tmp_path)
    (tmp_path / "salesfinder.yaml").write_text(
        "ops:\n  beat_seconds: 30\n  worker_stale_seconds: 60\n"
    )
    assert load_config(tmp_path).ops.worker_stale_seconds == 60


@pytest.mark.parametrize(
    "yaml_text",
    [
        "usage:\n  heartbeat_interval_minutes: 0\n",
        "usage:\n  heartbeat_interval_minutes: 1441\n",
        "ops:\n  beat_seconds: 4\n",
        "ops:\n  beat_seconds: 301\n  worker_stale_seconds: 900\n",
        "ops:\n  runs_keep_days: 0\n",
        "ops:\n  runs_keep_days: 366\n",
        "ops:\n  max_run_lines: 9\n",
        "ops:\n  max_run_lines: 2001\n",
        "ops:\n  surprise: 1\n",
    ],
)
def test_ops_and_heartbeat_bounds(tmp_path: Path, yaml_text: str) -> None:
    (tmp_path / "salesfinder.yaml").write_text(yaml_text)
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_ops_bounds_inclusive(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text(
        "usage:\n  heartbeat_interval_minutes: 1440\n"
        "ops:\n  beat_seconds: 5\n  worker_stale_seconds: 10\n"
        "  runs_keep_days: 365\n  max_run_lines: 10\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.usage.heartbeat_interval_minutes == 1440
    assert (cfg.ops.beat_seconds, cfg.ops.worker_stale_seconds) == (5, 10)
    assert (cfg.ops.runs_keep_days, cfg.ops.max_run_lines) == (365, 10)


@pytest.mark.parametrize(
    ("yaml_text", "zone"),
    [
        ('usage:\n  quiet_hours:\n    start: "22:00Z"\n', "quiet_hours.timezone"),
        ('usage:\n  quiet_hours:\n    end: "07:00+05:30"\n', "quiet_hours.timezone"),
        ('sending:\n  office_start: "09:00Z"\n', "the recipient's local time"),
        ('sending:\n  office_end: "17:00-05:00"\n', "the recipient's local time"),
    ],
)
def test_offset_aware_times_are_refused(tmp_path: Path, yaml_text: str, zone: str) -> None:
    # An aware time would pass validation and then raise TypeError in in_quiet_hours (or the
    # office-hours check) at runtime.
    (tmp_path / "salesfinder.yaml").write_text(yaml_text)
    with pytest.raises(ConfigError, match=f"give a wall-clock HH:MM; the zone is {zone}"):
        load_config(tmp_path)


def test_worker_stale_seconds_upper_bound(tmp_path: Path) -> None:
    (tmp_path / "salesfinder.yaml").write_text("ops:\n  worker_stale_seconds: 86401\n")
    with pytest.raises(ConfigError, match="worker_stale_seconds"):
        load_config(tmp_path)
    (tmp_path / "salesfinder.yaml").write_text("ops:\n  worker_stale_seconds: 86400\n")
    assert load_config(tmp_path).ops.worker_stale_seconds == 86400


SHIPPED_FILES = ["catalog.yaml", "facts.yaml", "salesfinder.yaml"]


def test_tests_never_read_a_local_config_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shipped_config_dir: Path,
    shipped_config: AppConfig,
) -> None:
    """Task 6 fix I1: an operator's salesfinder.local.yaml in the repo's config directory
    (git-ignored; the README tells them to make one) never reaches the tests' config: the
    fixtures copy only the shipped files, so even a broken override changes nothing."""
    import conftest

    repo = tmp_path / "repo-config"
    repo.mkdir()
    for name in SHIPPED_FILES:
        (repo / name).write_text((conftest.SHIPPED_CONFIG / name).read_text("utf-8"), "utf-8")
    (repo / "salesfinder.local.yaml").write_text(
        'usage:\n  quiet_hours:\n    start: "00:00"\n    end: "23:59"\nsending: [broken\n',
        "utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(repo)  # the hostile override is live for anything reading this directory
    monkeypatch.setattr(conftest, "SHIPPED_CONFIG", repo)

    dest = tmp_path / "copy"
    dest.mkdir()
    conftest.copy_shipped_config(dest, include_app_config=True)

    assert sorted(p.name for p in dest.iterdir()) == SHIPPED_FILES
    assert load_config(dest) == shipped_config
    assert sorted(p.name for p in shipped_config_dir.iterdir()) == SHIPPED_FILES


def test_brain_timeout_fits_the_worker_exit_timeout(tmp_path: Path) -> None:
    """Final wave T7: a stopping worker may wait for 4 Claude calls; with the margin they must
    end before launchd's 600 s ExitTimeOut SIGKILLs it."""
    (tmp_path / "salesfinder.yaml").write_text("brain:\n  timeout_seconds: 134.9\n")
    assert load_config(tmp_path).brain.timeout_seconds == 134.9
    (tmp_path / "salesfinder.yaml").write_text("brain:\n  timeout_seconds: 135\n")
    with pytest.raises(ConfigError, match=r"brain\.timeout_seconds: Input should be less than 135"):
        load_config(tmp_path)
    # The bound comes from the shared constants (the plist's ExitTimeOut among them).
    from salesfinder.config import MAX_BRAIN_TIMEOUT_SECONDS, BrainConfig

    assert MAX_BRAIN_TIMEOUT_SECONDS == 135
    schema = BrainConfig.model_json_schema()["properties"]["timeout_seconds"]
    assert schema["exclusiveMaximum"] == 135 and "maximum" not in schema
