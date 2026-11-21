"""The instructions a message was written from: the gate's ``prompt_text`` for its echo check.

First emails and follow-ups were written by ``writer.compose.prompt_for(build_context(...))``;
the prompt is rebuilt read-only (no Claude call). Replies (Claude's drafts and teammates'
answers alike) get the static reply prompt plus the side's playbook
(``replies.draft.instructions_for``).

Raises ``ValueError`` whose text is shown as the gate problem (a persona no longer confirmed,
the company on the other side, ...)."""

from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.config import AppConfig
from salesfinder.db.models import Campaign, Company, Message, Person
from salesfinder.replies.draft import instructions_for as reply_instructions
from salesfinder.writer.compose import prompt_for
from salesfinder.writer.context import build_context

__all__ = ["instructions_for"]


async def instructions_for(
    db: AsyncSession,
    *,
    message: Message,
    campaign: Campaign,
    company: Company,
    person: Person | None,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
) -> str:
    """The instructions ``message`` was written from (see the module docstring)."""
    if message.kind == "reply":
        side: Side = "supplier" if campaign.side == "supplier" else "buyer"
        return reply_instructions(side)
    inp = await build_context(
        db,
        campaign=campaign,
        company=company,
        person=person,
        catalog=catalog,
        facts=facts,
        config=config,
    )
    return prompt_for(inp)
