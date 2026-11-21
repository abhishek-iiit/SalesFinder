"""Schema-validated JSON tasks over the isolated runtime: budget check, one repair retry, and a
``usage_events`` row for every call."""

import json
import time
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.brain.budget import BudgetReached, record_usage, remaining_calls
from salesfinder.brain.runtime import UNEXPECTED, BrainError, BrainUnavailable, ClaudeRuntime
from salesfinder.config import AppConfig

REPAIR_SUFFIX = (
    "\n\nYour previous reply was not valid JSON for the schema. Reply with only the JSON object."
)
_ATTEMPTS = 2


def _balanced_end(text: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``start`` (JSON-string aware), or None."""
    depth = 0
    in_string = escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """The first balanced top-level ``{...}`` in ``text`` that parses as a JSON object,
    tolerating surrounding prose and ```json fences. Linear: each candidate scan resumes after
    the previous one."""
    start = text.find("{")
    while start != -1:
        end = _balanced_end(text, start)
        if end is None:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except (ValueError, RecursionError):  # model output may be pathologically deep
            value = None
        if isinstance(value, dict):
            return value
        start = text.find("{", end + 1)
    return None


def _validate[T: BaseModel](text: str, schema: type[T]) -> T | None:
    obj = extract_json_object(text)
    if obj is None:
        return None
    try:
        return schema.model_validate(obj)
    except (ValidationError, RecursionError):
        return None  # never surface pydantic's message: it quotes the (untrusted) input


async def complete_json[T: BaseModel](
    runtime: ClaudeRuntime,
    db_factory: async_sessionmaker[AsyncSession],
    *,
    prompt: str,
    schema: type[T],
    task: str,
    now: datetime,
    config: AppConfig,
    campaign_id: uuid.UUID | None = None,
) -> T:
    """Ask Claude for one JSON object matching ``schema``. Refuses when the daily budget is spent
    (before each attempt, so the repair retry is budgeted too); retries once with a short repair
    suffix; records every call made. ``BrainUnavailable`` means nothing was sent: not recorded."""
    cap = config.usage.max_daily_claude_calls
    for attempt in range(_ATTEMPTS):
        async with db_factory() as db:
            remaining = await remaining_calls(db, now=now, config=config)
        if remaining <= 0:
            raise BudgetReached(cap)

        attempt_prompt = prompt if attempt == 0 else prompt + REPAIR_SUFFIX
        started = time.monotonic()
        try:
            result = await runtime.complete(attempt_prompt)
        except BrainUnavailable:
            raise
        except BrainError as exc:
            await record_usage(
                db_factory,
                task=task,
                ok=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                result=None,
                error=exc.reason,
                campaign_id=campaign_id,
                at=now,
            )
            raise

        # Recorded whatever validation does: the call was made and counts against the budget.
        parsed: T | None = None
        try:
            parsed = _validate(result.text, schema)
        finally:
            await record_usage(
                db_factory,
                task=task,
                ok=parsed is not None,
                duration_ms=result.duration_ms,
                result=result,
                error=None if parsed is not None else UNEXPECTED,
                campaign_id=campaign_id,
                at=now,
            )
        if parsed is not None:
            return parsed
    raise BrainError(UNEXPECTED)
