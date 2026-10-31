"""Adapter of the notifications port (smtp): a recording implementation until the real transport is decided."""


class RecordingNotificationsAdapter:
    """Records what would have been sent; replace with the smtp client once the integration is specified."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
