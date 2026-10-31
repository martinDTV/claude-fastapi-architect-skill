"""HTTP adapter of the payments port: one client per call with an explicit timeout; errors reach the use case."""

import httpx


class HttpPaymentsAdapter:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def send(self, payload: dict[str, object]) -> None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_s) as client:
            response = await client.post("/events", json=payload)
            response.raise_for_status()


class RecordingPaymentsAdapter:
    """Development and test adapter: records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
