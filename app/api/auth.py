"""Shared API key check.

/health stays open. Every /v1 route uses this dependency.
"""

import hmac
import os
from typing import Annotated

from fastapi import Header, HTTPException


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("API_KEY", "")
    provided = x_api_key or ""
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
