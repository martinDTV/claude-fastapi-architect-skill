"""Hand-written dependencies."""

from fastapi import Header, HTTPException

API_KEY = "workshop-key"   # a demo key; real projects read it from their settings


def require_api_key(x_api_key: str = Header(default="")) -> str:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="missing or wrong api key")
    return x_api_key
