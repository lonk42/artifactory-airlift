"""Shared retry policy for object-storage calls.

Deliberately the same shape as ``artifactory_client._RETRY`` so operators see
one consistent backoff behaviour across every remote call airlift makes.
"""

from __future__ import annotations

import httpx
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

RETRY = dict(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
)
