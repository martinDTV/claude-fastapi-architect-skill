"""Ports of the billing module: what the use cases need from the outside, as Protocols."""

from typing import Protocol


class NotificationsPort(Protocol):
    """smtp integration (outbound, criticality low)."""

    async def send(self, payload: dict[str, object]) -> None: ...
