"""A programmable DNS resolver for ``MxChecker``: answers, dnspython errors, delays. No network."""

import asyncio
from typing import Any

Answer = list[tuple[int, str]] | BaseException


class FakeResolver:
    """``answers[(qname, rdtype)]`` is a list of (preference, host) or an exception to raise.

    Unknown (qname, rdtype) pairs raise ``dns.resolver.NoAnswer``. Tracks calls and the maximum
    number of lookups in flight.
    """

    def __init__(self, answers: dict[tuple[str, str], Answer] | None = None, delay: float = 0.0):
        self.answers: dict[tuple[str, str], Answer] = dict(answers or {})
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, qname: str, rdtype: str) -> list[tuple[int, str]]:
        import dns.resolver

        self.calls.append((qname, rdtype))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            answer: Any = self.answers.get((qname, rdtype), dns.resolver.NoAnswer())
            if isinstance(answer, BaseException):
                raise answer
            return list(answer)
        finally:
            self.in_flight -= 1
