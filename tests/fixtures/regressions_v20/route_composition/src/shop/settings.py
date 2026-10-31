"""A prefix that is not a constant: the auditor must not guess it."""

import os


class Settings:
    prefix = os.environ.get("SHOP_DYNAMIC_PREFIX", "/dynamic")


settings = Settings()
