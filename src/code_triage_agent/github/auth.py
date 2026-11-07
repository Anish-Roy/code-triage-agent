"""GitHub App authentication: App JWT -> installation access token cache."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_REFRESH_SKEW = timedelta(seconds=60)


class GitHubAuthError(RuntimeError):
    """Raised when GitHub rejects the App JWT or token exchange."""


@runtime_checkable
class TokenProvider(Protocol):
    """Anything the API client can ask for a bearer token."""

    def get_token(self, *, force_refresh: bool = False) -> str: ...

    def invalidate(self) -> None: ...


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1)
    expires_at: datetime


class GitHubAppAuth:
    """Mint a short-lived App JWT and exchange it for an installation token.

    The token is cached until shortly before expiry. The clock is injectable so
    tests can exercise expiry and refresh without sleeping.
    """

    def __init__(
        self,
        *,
        app_id: int,
        private_key: str,
        installation_id: int,
        client: httpx.Client | None = None,
        api_url: str = DEFAULT_API_URL,
        clock: Callable[[], datetime] | None = None,
        refresh_skew: timedelta = DEFAULT_REFRESH_SKEW,
    ) -> None:
        if not private_key.strip():
            raise GitHubAuthError("GitHub App private key is empty")
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._api_url = api_url.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._refresh_skew = refresh_skew
        self._client = client or httpx.Client(timeout=30.0)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def app_jwt(self) -> str:
        """Build a 9-minute RS256 JWT. GitHub allows at most 10 minutes."""
        now = int(self._clock().timestamp())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": str(self._app_id),
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._token is not None
                and self._expires_at is not None
                and now < self._expires_at - self._refresh_skew
            ):
                return self._token

            token, expires_at = self._exchange()
            self._token = token
            self._expires_at = expires_at
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    def _exchange(self) -> tuple[str, datetime]:
        url = f"{self._api_url}/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"token exchange failed: {exc}") from exc

        if response.status_code != 201:
            detail = response.text[:500]
            raise GitHubAuthError(f"token exchange returned {response.status_code}: {detail}")
        parsed = _TokenResponse.model_validate(response.json())
        expires_at = parsed.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return parsed.token, expires_at
