import httpx
import requests

shared_client = httpx.AsyncClient(base_url="https://api.invalid")


def fetch_rates() -> dict:
    return requests.get("https://rates.invalid/latest").json()                   # truth: REL_HTTP_CALL_WITHOUT_TIMEOUT


def fetch_rates_bounded() -> dict:
    return requests.get("https://rates.invalid/latest", timeout=5).json()        # truth-absent: REL_HTTP_CALL_WITHOUT_TIMEOUT


def unbounded_client() -> httpx.Client:
    return httpx.Client(timeout=None)                                            # truth: REL_HTTP_CALL_WITHOUT_TIMEOUT


def default_client() -> httpx.Client:
    return httpx.Client()                                                        # truth-absent: REL_HTTP_CALL_WITHOUT_TIMEOUT
