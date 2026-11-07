from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from code_triage_agent.github.auth import GitHubAppAuth, GitHubAuthError

TOKEN_URL = "https://api.github.com/app/installations/2/access_tokens"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="module")
def rsa_pair() -> tuple[str, object]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_pem, key.public_key()


def _auth(
    private_pem: str,
    clock: Callable[[], datetime],
    client: httpx.Client,
) -> GitHubAppAuth:
    return GitHubAppAuth(
        app_id=123,
        private_key=private_pem,
        installation_id=2,
        client=client,
        clock=clock,
    )


def test_app_jwt_has_expected_claims(rsa_pair: tuple[str, object]) -> None:
    private_pem, public_key = rsa_pair
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    auth = _auth(private_pem, Clock(now), httpx.Client())

    token = auth.app_jwt()
    claims = jwt.decode(
        token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] == 10 * 60


@respx.mock
def test_token_is_cached_until_near_expiry(rsa_pair: tuple[str, object]) -> None:
    private_pem, _ = rsa_pair
    clock = Clock(datetime(2026, 11, 7, 12, 0, tzinfo=UTC))
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            201,
            json={"token": "installation-token", "expires_at": "2026-11-07T12:30:00Z"},
        )
    )
    client = httpx.Client()
    auth = _auth(private_pem, clock, client)

    assert auth.get_token() == "installation-token"
    assert auth.get_token() == "installation-token"
    assert route.call_count == 1

    clock.now = datetime(2026, 11, 7, 12, 29, 30, tzinfo=UTC)
    assert auth.get_token() == "installation-token"
    assert route.call_count == 2
    client.close()


@respx.mock
def test_invalidate_forces_refresh(rsa_pair: tuple[str, object]) -> None:
    private_pem, _ = rsa_pair
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(201, json={"token": "one", "expires_at": "2026-11-07T12:30:00Z"}),
            httpx.Response(201, json={"token": "two", "expires_at": "2026-11-07T12:30:00Z"}),
        ]
    )
    client = httpx.Client()
    auth = _auth(private_pem, Clock(datetime(2026, 11, 7, 12, 0, tzinfo=UTC)), client)

    assert auth.get_token() == "one"
    auth.invalidate()
    assert auth.get_token() == "two"
    assert route.call_count == 2
    client.close()


@respx.mock
def test_rejected_token_exchange_raises(rsa_pair: tuple[str, object]) -> None:
    private_pem, _ = rsa_pair
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, text="bad credentials"))
    client = httpx.Client()
    auth = _auth(private_pem, Clock(datetime(2026, 11, 7, 12, 0, tzinfo=UTC)), client)

    with pytest.raises(GitHubAuthError, match="401"):
        auth.get_token()
    client.close()


def test_empty_private_key_is_rejected() -> None:
    with pytest.raises(GitHubAuthError, match="empty"):
        GitHubAppAuth(app_id=1, private_key="  ", installation_id=2)
