import requests


def legacy_rates() -> dict:
    return requests.get("https://carrier.invalid/rates").json()                       # truth: REL_HTTP_CALL_WITHOUT_TIMEOUT
