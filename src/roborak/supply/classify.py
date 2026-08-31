"""Which trust boundary a changed path sits on.

Pure path matching, deliberately. Content sniffing would make the answer depend on
which side of the change you looked at, and the whole stage is built on comparing
two sides. Globs go through ``compressor.matches_any`` so ``**/`` behaves here
exactly as it does in ``ignore_paths`` and in rule ``paths``.
"""

from __future__ import annotations

from roborak.context.compressor import matches_any
from roborak.core.models import AssetKind

_PATTERNS: list[tuple[AssetKind, tuple[str, ...]]] = [
    (
        AssetKind.CI_WORKFLOW,
        (
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
            ".github/actions/*action.yml",
            ".github/actions/*action.yaml",
            "**/.gitlab-ci.yml",
            "**/.gitlab-ci.yaml",
            ".gitlab/ci/*.yml",
            ".gitlab/ci/*.yaml",
            "**/azure-pipelines.yml",
            "**/.circleci/config.yml",
            "**/Jenkinsfile",
        ),
    ),
    (
        AssetKind.CONTAINER,
        (
            "**/Dockerfile",
            "**/Dockerfile.*",
            "**/*.dockerfile",
            "**/Containerfile",
            "**/docker-compose.yml",
            "**/docker-compose.yaml",
            "**/compose.yml",
            "**/compose.yaml",
        ),
    ),
    (
        AssetKind.IAC,
        (
            "**/*.tf",
            "**/*.tfvars",
            "**/*.hcl",
            "**/*.tf.json",
            "**/serverless.yml",
            "**/serverless.yaml",
            "**/template.yaml",
            "**/cloudformation/*.yml",
            "**/cloudformation/*.yaml",
            "**/helm/*values.yaml",
            "**/charts/*values.yaml",
            "**/k8s/*.yaml",
            "**/k8s/*.yml",
            "**/kubernetes/*.yaml",
            "**/kubernetes/*.yml",
            "**/manifests/*.yaml",
        ),
    ),
    (
        AssetKind.PACKAGE_MANAGER_CONFIG,
        (
            "**/.npmrc",
            "**/.yarnrc",
            "**/.yarnrc.yml",
            "**/pip.conf",
            "**/pip.ini",
            "**/.cargo/config.toml",
            "**/.cargo/config",
            "**/.netrc",
            "**/nuget.config",
            "**/.bundle/config",
        ),
    ),
]
"""Boundaries recognised by path alone, most specific first.

    A single ``*`` already crosses directory separators here, because that is how
    ``fnmatch`` -- and therefore every other glob in roborak -- behaves. A ``**``
    in the middle of a pattern would demand an intermediate directory that need
    not exist, so ``k8s/*.yaml`` is what matches both ``k8s/deploy.yaml`` and
    ``k8s/base/deploy.yaml``.

``CI_WORKFLOW`` is checked before ``IAC`` on purpose: a workflow is YAML in a
directory that a broad Kubernetes glob would happily claim, and a file gets
exactly one kind."""


def classify(path: str) -> AssetKind | None:
    """The boundary ``path`` sits on, or ``None`` when it is ordinary code.

    Dependency manifests and lockfiles are not listed above: they come from the
    ecosystem parsers, which own the names they can actually read, so a manifest
    this package cannot parse is never claimed as one it can.
    """
    from roborak.supply.ecosystems import kind_for_dependency_file

    if (dependency := kind_for_dependency_file(path)) is not None:
        return dependency
    for kind, patterns in _PATTERNS:
        if matches_any(path, patterns):
            return kind
    return None
