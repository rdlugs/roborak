"""Which deployment and runtime surfaces a change actually touches.

Operational defects -- a migration that locks a big table, a retry around a
non-idempotent write, a flag that defaults on -- are locally correct code. The
reviewer only finds them if it is told to look, and telling it to look on every
diff would buy a permanent tax in tokens and a steady drip of "add monitoring"
on changes that deploy no differently than the last one.

So the checklist is gated the way the supply-chain one is (``supply.prompt``):
a cheap pass over the change names the surfaces it crosses, and only those
sub-checklists reach the prompt. An empty list means no section at all.

Detection reads paths and *changed* lines only. A keyword sitting in untouched
context is not evidence that this change touches a queue.
"""

from __future__ import annotations

import re

from roborak.core.models import ChangedFile, ChangeSet, Hunk

MIGRATION = "migration"
DEPLOYMENT = "deployment"
PUBLIC_CONTRACT = "public_contract"
BACKGROUND_JOB = "background_job"
RETRY_TIMEOUT = "retry_timeout"
FEATURE_FLAG = "feature_flag"
RESOURCE_LIMITS = "resource_limits"
CACHE = "cache"
OBSERVABILITY = "observability"

# ``context.chunker`` carries overlapping path regexes, but it deliberately
# lumps migrations, schemas and deploy config into one SCHEMA_CONFIG role.
# Gating needs them apart -- a Terraform diff wants the deploy checklist and not
# the migration one -- so these are kept separate rather than shared.
_PATH_PATTERNS: dict[str, re.Pattern[str]] = {
    MIGRATION: re.compile(
        r"(^|/)(migrations?|migrate|alembic|liquibase|flyway)(/|$)"
        # A bare .sql file is as often a query or a fixture as a migration; only
        # one named for the schema it changes is evidence on its own.
        r"|(^|/)[^/]*(migrat|schema)[^/]*\.sql$",
        re.IGNORECASE,
    ),
    DEPLOYMENT: re.compile(
        r"(^|/)(k8s|kubernetes|deploy|deployment|helm|charts?|terraform|infra|ansible)(/|$)"
        r"|(^|/)\.github/workflows/"
        r"|(^|/)(dockerfile[^/]*|compose(\.[^.]+)?\.ya?ml|values(\.[^.]+)?\.ya?ml)$"
        r"|\.(tf|tfvars)$",
        re.IGNORECASE,
    ),
    PUBLIC_CONTRACT: re.compile(
        r"(^|/)(api|apis|routes?|endpoints?|interfaces?|contracts?|schemas?|proto)(/|$)"
        r"|(^|/)(openapi|swagger)[^/]*\.(ya?ml|json)$"
        r"|(^|/)(routes?|api|schema|serializers?)\.[^/]+$"
        r"|\.proto$",
        re.IGNORECASE,
    ),
    BACKGROUND_JOB: re.compile(
        r"(^|/)(jobs?|workers?|tasks?|queues?|consumers?|cron|schedulers?)(/|$)",
        re.IGNORECASE,
    ),
    # A file named for flags is one, whatever it happens to call the constant.
    FEATURE_FLAG: re.compile(
        r"(^|/)(feature_?flags?|flags?|feature_?toggles?|toggles?)(/|\.[^/]+$)",
        re.IGNORECASE,
    ),
    CACHE: re.compile(
        r"(^|/)(caches?|caching)(/|\.[^/]+$)",
        re.IGNORECASE,
    ),
}

_CONTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    BACKGROUND_JOB: re.compile(
        r"\b(celery|sidekiq|resque|bullmq|kafka|rabbitmq|amqp|sqs|pubsub|shared_task|"
        r"apply_async|enqueue|dequeue|perform_later|crontab|cron_schedule)\b",
        re.IGNORECASE,
    ),
    RETRY_TIMEOUT: re.compile(
        r"\b(retry|retries|retrying|backoff|timeout|timeouts|deadline|max_attempts|"
        r"max_retries|tenacity|circuit_breaker)\b",
        re.IGNORECASE,
    ),
    FEATURE_FLAG: re.compile(
        r"\b(feature_?flags?|feature_?toggles?|launchdarkly|unleash|flagsmith|flipper|"
        r"flag_enabled|is_feature_enabled)\b",
        re.IGNORECASE,
    ),
    RESOURCE_LIMITS: re.compile(
        r"\b(max_connections|pool_size|connection_pool|max_pool_size|replicas|"
        r"rate_limits?|max_workers|concurrency|memory_limit|cpu_limit|batch_size|"
        r"semaphore)\b",
        re.IGNORECASE,
    ),
    # Kept apart from the pools and limits above: a cache fails by serving the
    # wrong answer or by not being there, which the limits checklist never asks.
    # Bounded on identifiers rather than words: ``\b`` counts ``_`` as a word
    # character, so the compound names cache markers usually live in --
    # ``DEFAULT_CACHE_TTL``, ``order_cache_key`` -- would go unseen.
    CACHE: re.compile(
        r"(?<![0-9A-Za-z])(cache|caches|cached|caching|cache_key|cache_ttl|invalidate|"
        r"invalidation|memcache|memcached|redis|lru_cache|memoize|memoized|ttl)"
        r"(?![0-9A-Za-z])",
        re.IGNORECASE,
    ),
}

# Losing a signal is the failure mode worth a checklist; adding one is not. An
# added log line on an otherwise ordinary change must not drag in a section
# whose only possible finding would be operational ceremony.
#
# These fire on the removed body, which has no new-file line of its own: a
# finding about a deleted log can only be anchored to a line that survives the
# change. So they are gated per hunk -- the removal and the added line it would
# anchor to must be in the same hunk. A removal whose file only adds somewhere
# else entirely leaves nothing near enough to carry a finding past
# ``anchor_to_changed_lines``, and asking for one would buy tokens for output
# that is discarded.
_REMOVED_ONLY_PATTERNS: dict[str, re.Pattern[str]] = {
    OBSERVABILITY: re.compile(
        r"\b(logger|logging|log_|metrics?|prometheus|statsd|datadog|opentelemetry|otel|"
        # `span` alone is markup far more often than tracing.
        r"tracer|tracing|start_span|start_as_current_span|sentry|alerts?)\b",
        re.IGNORECASE,
    ),
}


def operational_signals(changeset: ChangeSet) -> list[str]:
    """The deployment and runtime surfaces this change crosses, sorted.

    Empty means the change touches none of them, and the reviewer is asked
    nothing about rollout.
    """
    found: set[str] = set()
    for file in changeset.files:
        for kind, pattern in _PATH_PATTERNS.items():
            if pattern.search(file.path):
                found.add(kind)

        added, removed = _changed_lines(file)
        added_body, removed_body = "\n".join(added), "\n".join(removed)
        for kind, pattern in _CONTENT_PATTERNS.items():
            if pattern.search(added_body) or pattern.search(removed_body):
                found.add(kind)
        for hunk in file.hunks:
            hunk_added, hunk_removed = _hunk_lines(hunk)
            if not hunk_added:
                continue
            hunk_removed_body = "\n".join(hunk_removed)
            for kind, pattern in _REMOVED_ONLY_PATTERNS.items():
                if pattern.search(hunk_removed_body):
                    found.add(kind)
    return sorted(found)


def _changed_lines(file: ChangedFile) -> tuple[list[str], list[str]]:
    """The added and removed lines of every hunk, without their diff markers."""
    added: list[str] = []
    removed: list[str] = []
    for hunk in file.hunks:
        hunk_added, hunk_removed = _hunk_lines(hunk)
        added.extend(hunk_added)
        removed.extend(hunk_removed)
    return added, removed


def _hunk_lines(hunk: Hunk) -> tuple[list[str], list[str]]:
    """The added and removed lines of one hunk, without their diff markers."""
    added: list[str] = []
    removed: list[str] = []
    for line in hunk.content.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed
