"""Ports of the orders module: what the use cases need from the outside, as Protocols."""

from typing import Protocol


class PaymentsPort(Protocol):
    """http integration (outbound, criticality high)."""

    async def send(self, payload: dict[str, object]) -> None: ...
