"""Shared plumbing for talking to GitLab and GitHub.

One client for both providers so auth, retries, pagination and error reporting
behave identically no matter where a change came from.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

from roborak.sources.base import SourceError

log = logging.getLogger(__name__)

Provider = Literal["gitlab", "github"]

DEFAULT_TIMEOUT = 30.0
MAX_PAGES = 50
"""Pagination stops here; a PR with more pages than this is not reviewable anyway."""


@dataclass
class Target:
    """A parsed reference to a merge request or pull request."""

    provider: Provider
    host: str
    project: str
    number: int

    @property
    def api_base(self) -> str:
        if self.provider == "gitlab":
            return f"https://{self.host}/api/v4"
        if self.host in {"github.com", "www.github.com"}:
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"  # GitHub Enterprise

    @property
    def encoded_project(self) -> str:
        """GitLab addresses projects by URL-encoded path."""
        return quote(self.project, safe="")


_GITLAB_URL = re.compile(r"^https?://([^/]+)/(.+?)/-/merge_requests/(\d+)")
_GITHUB_URL = re.compile(r"^https?://([^/]+)/([^/]+/[^/]+)/pull/(\d+)")


def parse_target(
    reference: str, provider: Provider, *, host: str | None = None, project: str | None = None
) -> Target:
    """Accept either a full URL or a bare number plus explicit project details."""
    if reference.startswith(("http://", "https://")):
        pattern = _GITLAB_URL if provider == "gitlab" else _GITHUB_URL
        match = pattern.match(reference)
        if not match:
            raise SourceError(f"Could not parse {provider} URL: {reference}")
        return Target(provider, match.group(1), match.group(2), int(match.group(3)))

    if not reference.isdigit():
        raise SourceError(f"Expected a number or a URL, got: {reference}")

    resolved_host = host or ("gitlab.com" if provider == "gitlab" else "github.com")
    resolved_project = project or detect_project(provider)
    if not resolved_project:
        raise SourceError(
            f"Could not work out which {provider} project to use. "
            f"Pass a full URL, or set a git remote."
        )
    return Target(provider, resolved_host, resolved_project, int(reference))


def detect_project(provider: Provider, remote: str = "origin") -> str | None:
    """Read the project path out of the repository's git remote."""
    if not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "remote", "get-url", remote], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    return project_from_remote(result.stdout.strip())


def project_from_remote(url: str) -> str | None:
    """Extract ``group/project`` from an SSH or HTTPS git remote."""
    url = url.strip().removesuffix(".git")
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        path = urlparse(url).path.strip("/")
        return path or None
    # scp-style: git@host:group/project
    if ":" in url and "@" in url:
        return url.split(":", 1)[1].strip("/") or None
    return None


def detect_host(provider: Provider, remote: str = "origin") -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", remote], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip().removesuffix(".git")
    if url.startswith(("http://", "https://")):
        return urlparse(url).netloc or None
    if "@" in url and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0] or None
    return None


def get_token(provider: Provider) -> str | None:
    """Find a token, preferring an explicit env var over the gh CLI's session."""
    names = (
        ("ROBORAK_GITLAB_TOKEN", "GITLAB_TOKEN", "CI_JOB_TOKEN")
        if provider == "gitlab"
        else ("ROBORAK_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    )
    for name in names:
        if token := os.getenv(name):
            return token

    if provider == "github" and shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and (token := result.stdout.strip()):
            return token
    return None


class ForgeClient:
    """A thin, synchronous REST client with the auth header each provider wants."""

    def __init__(self, target: Target, token: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.target = target
        headers = (
            {"PRIVATE-TOKEN": token}
            if target.provider == "gitlab"
            else {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        self._client = httpx.Client(
            base_url=target.api_base,
            headers={**headers, "User-Agent": "roborak"},
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> ForgeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, json=payload)

    def paginate(self, path: str, **params: Any) -> list[Any]:
        """Walk every page, stopping at ``MAX_PAGES`` rather than looping forever."""
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._request("GET", path, params={**params, "page": page, "per_page": 100})
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise SourceError(f"Could not reach {self.target.host}: {exc}") from exc

        if response.status_code == 401:
            raise SourceError(
                f"{self.target.host} rejected the token. Check the relevant "
                f"{'GITLAB_TOKEN' if self.target.provider == 'gitlab' else 'GITHUB_TOKEN'}."
            )
        if response.status_code == 403:
            raise SourceError(f"Not permitted: {method} {path} on {self.target.host}.")
        if response.status_code == 404:
            raise SourceError(
                f"Not found: {self.target.project} #{self.target.number} on {self.target.host}. "
                "Check the project path and that the token can see it."
            )
        if response.status_code >= 400:
            raise SourceError(
                f"{self.target.host} returned {response.status_code} for {method} {path}: "
                f"{response.text[:300]}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.target.host} returned a non-JSON response.") from exc
