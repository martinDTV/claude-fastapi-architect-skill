import requests


class EmailNotifier:
    """The only implementation of accounts.ports.NotifierPort."""

    def send(self, to: str, body: str) -> None:
        requests.post("https://mail.invalid/send", json={"to": to, "body": body}, timeout=5)
