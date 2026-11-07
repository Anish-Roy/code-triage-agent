"""Typed subsets of the GitHub REST API responses used by the collector."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GitHubPullRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int


class GitHubWorkflowRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    run_attempt: int = 1
    name: str | None = None
    event: str = ""
    status: str | None = None
    conclusion: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    html_url: str | None = None
    pull_requests: list[GitHubPullRef] = Field(default_factory=list)


class GitHubStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    name: str
    status: str | None = None
    conclusion: str | None = None


class GitHubJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    status: str | None = None
    conclusion: str | None = None
    steps: list[GitHubStep] = Field(default_factory=list)


class GitHubJobsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_count: int = 0
    jobs: list[GitHubJob] = Field(default_factory=list)


class GitHubBranchRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    sha: str


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    state: str
    merged: bool = False
    merge_commit_sha: str | None = None
    head: GitHubBranchRef
    base: GitHubBranchRef


class GitHubIssueComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
