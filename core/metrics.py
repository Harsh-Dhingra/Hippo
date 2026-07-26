"""Metrics that come from the database rather than from a counter in a process.

Most of this project's instrumentation is counters incremented where the work
happens: jobs claimed, chunks embedded, actions executed. Those are correct and
cheap, and they answer "what did this process do".

They cannot answer the questions an operator actually has at 3am, because those
are about *state* rather than about events:

    How far behind is each sync stream?
    How deep is the queue, and how much of it is dead?
    How many actions are sitting waiting for a human?
    Are any stuck mid-write?

A counter in a process cannot know those. They are properties of the database,
they survive restarts, and a process that has just booted knows none of its own
history. So they are collected at scrape time, by asking.

**A scrape must never fail the endpoint.** If the database is unreachable, the
answer is that these particular metrics are missing, not that /metrics returns
500 — because a monitoring endpoint that goes down with its dependency is a
monitoring endpoint that goes quiet exactly when it is needed. Failures are
reported as a gauge of their own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from core.db import Database

LOG = logging.getLogger("hippo.metrics")

SCRAPE_TIMEOUT = 3.0

# One query per metric family, each written to return a small, fixed-shape
# result. Nothing here scans a large table: every predicate is on an indexed
# column or a status, and the row counts are bounded by connectors and streams.
QUERIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "hippo_sync_lag_seconds": (
        "Seconds since this connector's stream last synced. Absent until it has run once.",
        "SELECT c.kind, s.stream, extract(epoch FROM now() - s.last_synced_at) "
        "FROM sync_state s JOIN connectors c ON c.id = s.connector_id "
        "WHERE s.last_synced_at IS NOT NULL",
        ("connector", "stream"),
    ),
    "hippo_sync_stream_failed": (
        "1 when this stream's last run recorded an error, 0 otherwise.",
        "SELECT c.kind, s.stream, CASE WHEN s.last_error IS NULL THEN 0 ELSE 1 END "
        "FROM sync_state s JOIN connectors c ON c.id = s.connector_id",
        ("connector", "stream"),
    ),
    "hippo_jobs_queue_depth": (
        "Jobs in the queue, by kind and status. Dead is the one to alert on.",
        "SELECT kind, status, count(*) FROM jobs GROUP BY kind, status",
        ("kind", "status"),
    ),
    "hippo_jobs_oldest_pending_seconds": (
        "Age of the oldest runnable job. Rises when workers are gone.",
        "SELECT kind, extract(epoch FROM now() - min(run_at)) FROM jobs "
        "WHERE status = 'pending' AND run_at <= now() GROUP BY kind",
        ("kind",),
    ),
    "hippo_actions_total": (
        "Actions by status. Pending is a queue of human decisions, not a backlog.",
        "SELECT action_type, status, count(*) FROM actions GROUP BY action_type, status",
        ("action_type", "status"),
    ),
    "hippo_actions_in_flight_seconds": (
        "Age of the oldest action mid-write. A rising value means an executor died.",
        "SELECT max(extract(epoch FROM now() - execution_started_at)) FROM actions "
        "WHERE status IN ('executing', 'rolling_back')",
        (),
    ),
    "hippo_tokens_total": (
        "Model tokens spent, from the trace log. Survives a restart; a counter does not.",
        "SELECT provider, coalesce(model, 'unknown'), "
        "       sum(input_tokens)::bigint, sum(output_tokens)::bigint "
        "FROM query_traces WHERE provider IS NOT NULL GROUP BY 1, 2",
        ("provider", "model"),
    ),
    "hippo_alerts_open": (
        "Unacknowledged alerts by kind. Schema drift and sync failure are silent "
        "by construction; this is what makes them countable.",
        "SELECT kind, count(*) FROM alerts WHERE acknowledged_at IS NULL GROUP BY kind",
        ("kind",),
    ),
    "hippo_alerts_oldest_seconds": (
        "Age of the oldest unacknowledged alert. Rising means nobody is looking.",
        "SELECT extract(epoch FROM now() - min(first_seen_at)) FROM alerts "
        "WHERE acknowledged_at IS NULL",
        (),
    ),
    "hippo_queries_total": (
        "Queries answered, by how they were routed. 'error' is the one to watch.",
        "SELECT route, count(*) FROM query_traces GROUP BY route",
        ("route",),
    ),
}

# The token query returns two values per row rather than one, so it is built
# separately. Kept in the same table above for the description and the labels.
TOKEN_METRIC = "hippo_tokens_total"


class DatabaseCollector(Collector):
    """Reports database state on every scrape.

    Registered once, at app startup. prometheus_client calls collect() when
    /metrics is requested, which is the only time these queries run.
    """

    def __init__(self, db: Database, timeout: float = SCRAPE_TIMEOUT) -> None:
        self._db = db
        self._timeout = timeout

    def collect(self) -> Iterator[Any]:
        failures = GaugeMetricFamily(
            "hippo_metrics_scrape_failed",
            "1 when the last scrape could not read the database.",
        )
        try:
            with self._db.connection(timeout=self._timeout) as conn:
                yield from self._families(conn)
        except Exception as exc:
            # Deliberately broad. A monitoring endpoint that goes down with its
            # dependency goes quiet exactly when someone is looking at it.
            LOG.warning("metrics scrape failed", extra={"error": str(exc)})
            failures.add_metric([], 1.0)
            yield failures
            return

        failures.add_metric([], 0.0)
        yield failures

    def _families(self, conn: Any) -> Iterable[Any]:
        for name, (description, sql, labels) in QUERIES.items():
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
            except Exception as exc:
                # One bad query — a table a migration has not created yet —
                # must not cost every other metric. The rollback is what makes
                # that true: a failed statement aborts the transaction, so
                # without it the first failure silently blanks the whole
                # dashboard rather than one panel.
                LOG.warning("metric query failed", extra={"metric": name, "error": str(exc)})
                conn.rollback()
                continue

            if name == TOKEN_METRIC:
                yield from _tokens(description, labels, rows)
                continue

            family = GaugeMetricFamily(name, description, labels=list(labels))
            for row in rows:
                value = row[-1]
                if value is None:
                    continue
                family.add_metric([str(item) for item in row[:-1]], float(value))
            yield family


def _tokens(description: str, labels: tuple[str, ...], rows: Iterable[Any]) -> Iterator[Any]:
    """Input and output tokens are priced differently, so they are separate
    series rather than one total nobody can turn into money."""
    families = {
        "input": GaugeMetricFamily("hippo_tokens_input_total", description, labels=list(labels)),
        "output": GaugeMetricFamily("hippo_tokens_output_total", description, labels=list(labels)),
    }
    for row in rows:
        tags = [str(item) for item in row[:-2]]
        families["input"].add_metric(tags, float(row[-2] or 0))
        families["output"].add_metric(tags, float(row[-1] or 0))
    yield from families.values()
