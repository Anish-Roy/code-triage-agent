"""GitHub integration package."""

from code_triage_agent.github.auth import GitHubAppAuth, GitHubAuthError, TokenProvider
from code_triage_agent.github.client import (
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)

__all__ = [
    "GitHubAppAuth",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubClientError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "TokenProvider",
]
