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
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

from roborak.core.config import ForgeConfig
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
    """Bare ``host[:port]``: it doubles as a state key and appears in every error."""

    project: str
    number: int
    scheme: str = "https"
    """``http`` only for an instance that says so; kept off ``host`` deliberately."""

    @property
    def api_base(self) -> str:
        if self.provider == "gitlab":
            return f"{self.scheme}://{self.host}/api/v4"
        if self.host in {"github.com", "www.github.com"}:
            return "https://api.github.com"
        return f"{self.scheme}://{self.host}/api/v3"

    @property
    def encoded_project(self) -> str:
        """GitLab addresses projects by URL-encoded path."""
        return quote(self.project, safe="")


_GITLAB_URL = re.compile(r"^(https?)://([^/]+)/(.+?)/-/merge_requests/(\d+)")
_GITHUB_URL = re.compile(r"^(https?)://([^/]+)/([^/]+/[^/]+)/pull/(\d+)")
_GITLAB_ISSUE_URL = re.compile(r"^(https?)://([^/]+)/(.+?)/-/issues/(\d+)")
_GITHUB_ISSUE_URL = re.compile(r"^(https?)://([^/]+)/([^/]+/[^/]+)/issues/(\d+)")

TargetKind = Literal["change", "issue"]
"""``change`` is a merge/pull request; ``issue`` is a tracker issue."""

_URL_PATTERNS: dict[tuple[TargetKind, Provider], re.Pattern[str]] = {
    ("change", "gitlab"): _GITLAB_URL,
    ("change", "github"): _GITHUB_URL,
    ("issue", "gitlab"): _GITLAB_ISSUE_URL,
    ("issue", "github"): _GITHUB_ISSUE_URL,
}


def parse_target(
    reference: str,
    provider: Provider,
    *,
    host: str | None = None,
    project: str | None = None,
    kind: TargetKind = "change",
    repo: Path | None = None,
) -> Target:
    """Accept either a full URL or a bare number plus explicit project details.

    ``kind`` selects which URL shape is acceptable, so a merge-request flag still
    rejects an issue URL rather than silently reviewing the wrong thing.
    """
    if reference.startswith(("http://", "https://")):
        match = _URL_PATTERNS[(kind, provider)].match(reference)
        if not match:
            raise SourceError(f"Could not parse {provider} URL: {reference}")
        scheme, url_host, url_project, number = match.groups()
        return Target(provider, url_host, url_project, int(number), scheme=scheme)

    if not reference.isdigit():
        raise SourceError(f"Expected a number or a URL, got: {reference}")

    scheme, resolved_host = split_host(
        host or ("gitlab.com" if provider == "gitlab" else "github.com")
    )
    resolved_project = project or detect_project(provider, repo=repo)
    if not resolved_project:
        raise SourceError(
            f"Could not work out which {provider} project to use. "
            f"Pass a full URL, or set a git remote."
        )
    return Target(provider, resolved_host, resolved_project, int(reference), scheme=scheme)


def _remote_url(remote: str, repo: Path | None) -> str | None:
    """The configured URL of ``remote``, read inside ``repo``.

    ``repo`` matters: ``-C other/repo`` must resolve that repository's remote, not
    whichever directory the process happens to have been started in.
    """
    if not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "remote", "get-url", remote],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect_project(
    provider: Provider, remote: str = "origin", *, repo: Path | None = None
) -> str | None:
    """Read the project path out of the repository's git remote."""
    url = _remote_url(remote, repo)
    return project_from_remote(url) if url else None


def project_from_remote(url: str) -> str | None:
    """Extract ``group/project`` from an SSH or HTTPS git remote."""
    url = url.strip().removesuffix(".git")
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        path = urlparse(url).path.strip("/")
        return path or None
    if ":" in url and "@" in url:
        return url.split(":", 1)[1].strip("/") or None
    return None


def split_host(value: str) -> tuple[str, str]:
    """Split ``[scheme://]host[:port]`` into its scheme and netloc, https by default."""
    for scheme in ("https", "http"):
        if value.lower().startswith(f"{scheme}://"):
            return scheme, value[len(scheme) + 3 :].rstrip("/")
    return "https", value


def detect_host(
    provider: Provider, remote: str = "origin", *, repo: Path | None = None
) -> str | None:
    """The host the git remote points at, as ``[scheme://]host[:port]``.

    The scheme is carried only when the remote is plain ``http``, which is a real
    shape for an internal instance and would otherwise be silently upgraded.
    """
    url = _remote_url(remote, repo)
    if url is None:
        return None
    url = url.removesuffix(".git")
    if url.startswith(("http://", "https://")):
        netloc = urlparse(url).netloc
        if not netloc:
            return None
        return f"http://{netloc}" if url.startswith("http://") else netloc
    if "@" in url and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0] or None
    return None


def resolve_host(
    provider: Provider, forge: ForgeConfig | None = None, *, repo: Path | None = None
) -> str | None:
    """Where this provider lives: the git remote first, then ``forge.hosts``.

    The remote is per-repository evidence and beats configuration on purpose -- a
    domain set user-wide must not hijack a checkout whose remote says otherwise.
    ``None`` leaves the caller on the provider's public default.
    """
    return detect_host(provider, repo=repo) or (forge.hosts.get(provider) if forge else None)


def provider_from_url(reference: str) -> Provider | None:
    """Which forge a full URL points at, judged by its path shape.

    GitLab namespaces everything under ``/-/``, which is what distinguishes
    ``.../-/issues/3`` from GitHub's ``.../issues/3``.
    """
    if not reference.startswith(("http://", "https://")):
        return None
    if _GITLAB_ISSUE_URL.match(reference) or _GITLAB_URL.match(reference):
        return "gitlab"
    if _GITHUB_ISSUE_URL.match(reference) or _GITHUB_URL.match(reference):
        return "github"
    return None


def detect_provider(
    remote: str = "origin", *, repo: Path | None = None, forge: ForgeConfig | None = None
) -> Provider | None:
    """Guess the forge from the git remote's host.

    A host named in ``forge.hosts`` settles it, which is how a self-hosted instance
    called something like ``git.acme.com`` becomes recognisable at all. Failing
    that, the name has to speak for itself: anything else returns ``None`` so the
    caller can ask for a full URL rather than pick one and be wrong.
    """
    host = split_host((detect_host("gitlab", remote, repo=repo) or "").lower())[1]
    if not host:
        return None
    if forge is not None:
        candidates: tuple[Provider, ...] = ("gitlab", "github")
        for candidate in candidates:
            configured = forge.hosts.get(candidate)
            if configured and split_host(configured.lower())[1] == host:
                return candidate
    if "gitlab" in host:
        return "gitlab"
    if "github" in host:
        return "github"
    return None


def get_token(provider: Provider, forge: ForgeConfig | None = None) -> str | None:
    """Find a token: configured first, then the environment, then the gh CLI.

    ``forge.tokens`` already carries ``ROBORAK_<PROVIDER>_TOKEN`` when it is set,
    since the environment is a config layer, so checking it first keeps the
    documented precedence of env over file.
    """
    if forge is not None and (configured := forge.tokens.get(provider)):
        return configured.get_secret_value()

    names = (
        ("GITLAB_TOKEN", "CI_JOB_TOKEN") if provider == "gitlab" else ("GITHUB_TOKEN", "GH_TOKEN")
    )
    for name in names:
        if token := os.getenv(name):
            return token

    if provider == "github" and shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
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

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", path, json=payload)

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PATCH", path, json=payload)

    def get_raw(self, path: str, **params: Any) -> bytes:
        """Fetch a raw repository blob while retaining the normal error handling."""
        response = self._send("GET", path, params=params)
        return response.content

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
        response = self._send(method, path, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.target.host} returned a non-JSON response.") from exc

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise SourceError(f"Could not reach {self.target.host}: {exc}") from exc

        if response.status_code == 401:
            raise SourceError(
                f"{self.target.host} rejected the token. Check "
                f"{'GITLAB_TOKEN' if self.target.provider == 'gitlab' else 'GITHUB_TOKEN'} "
                f"or forge.tokens.{self.target.provider} in the config.",
                status=401,
            )
        if response.status_code == 403:
            raise SourceError(f"Not permitted: {method} {path} on {self.target.host}.", status=403)
        if response.status_code == 404:
            raise SourceError(
                f"Not found: {self.target.project} #{self.target.number} on {self.target.host}. "
                "Check the project path and that the token can see it.",
                status=404,
            )
        if response.status_code >= 400:
            raise SourceError(
                f"{self.target.host} returned {response.status_code} for {method} {path}: "
                f"{response.text[:300]}",
                status=response.status_code,
            )

        return response
