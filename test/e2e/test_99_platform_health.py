"""Platform invariants, asserted last.

These are the checks no service's API exposes, and the reason the suite reads a database
directly. Each one is a statement about the whole platform after everything above has run:

* nothing was dead-lettered,
* every outbox drained,
* every balance still equals the sum of its ledger.

Named 99 so it runs after the flows it is judging.
"""

from __future__ import annotations

import time

import arcadia as a
import pytest

DEAD_LETTER_TOPICS = [
    "wallet-commands.dlq",
    "catalog-commands.dlq",
    "wallet-events.dlq",
    "game-events.dlq",
    "payment-events.dlq",
    "user-events.dlq",
    "trade-events.dlq",
    # The notification service dead-letters to one of these for every topic it reads, and it is
    # the only consumer of some of them. A notification that silently never arrives shows up here
    # and nowhere else, because nothing downstream is waiting on it to notice.
    "purchase-events.dlq",
    "festival-events.dlq",
    # Community's own topic. Search and Profile both build read-models from it, and a post
    # that never becomes findable is invisible everywhere else — the failure shows up here
    # first. `game-events.dlq` above now also covers community's consumer of that topic.
    "community-events.dlq",
]

# The Go and Python services have independently designed outbox tables — the Go one tracks
# `status` and `attempt_count`, the Python one a nullable `published_at` and `attempts`, and auth
# a `dispatched` boolean in a table called `outbox`. They are separate services with separate
# schemas, so the queries differ rather than the schemas being forced together.
#
# (database, table, unpublished, exhausted). `exhausted` is None where the schema does not count
# attempts, which is not a gap worth inventing a column for: an undispatched row shows up in the
# first check either way.
#
# notification-service is deliberately absent — it produces no events, so it has no outbox.
OUTBOXES = {
    "wallet": ("outbox_messages", "status <> 'PUBLISHED'", "attempt_count >= 10"),
    "payment": ("outbox_messages", "status <> 'PUBLISHED'", "attempt_count >= 10"),
    "catalog": ("outbox_messages", "published_at IS NULL", "attempts >= 10"),
    "order": ("outbox_messages", "published_at IS NULL", "attempts >= 10"),
    "media": ("outbox_messages", "published_at IS NULL", "attempts >= 10"),
    "auth": ("outbox", "dispatched = false", None),
    # Community follows the Python shape but calls the table `outbox`, not `outbox_messages`.
    "community": ("outbox", "published_at IS NULL", "attempts >= 10"),
}


@pytest.mark.parametrize("topic", DEAD_LETTER_TOPICS)
def test_the_dead_letter_topic_is_empty(topic: str):
    """Anything here is a message the platform could not process.

    For a command topic that means a contract violation; for an event topic it means a
    handler failed permanently. Either way somebody has to look, so a non-empty DLQ is a
    failure rather than a warning.
    """
    count = a.topic_message_count(topic)
    assert count == 0, (
        f"{count} message(s) in {topic}.\n"
        f"Read them with:  docker exec arcadia-kafka kafka-console-consumer.sh "
        f"--bootstrap-server localhost:9092 --topic {topic} --from-beginning --max-messages 5"
    )


@pytest.mark.parametrize("database", sorted(OUTBOXES))
def test_every_outbox_drained(database: str):
    """A backlog means the dispatcher is wedged or the broker is unreachable.

    Retried briefly before failing: the dispatcher polls on an interval, so a row written a
    moment ago still being unpublished is normal rather than broken.
    """
    table, pending_clause, _ = OUTBOXES[database]
    deadline = time.monotonic() + 15
    unpublished = None
    while time.monotonic() < deadline:
        unpublished = int(a.psql(database, f"SELECT count(*) FROM {table} WHERE {pending_clause}"))
        if unpublished == 0:
            return
        time.sleep(1)
    pytest.fail(f"{unpublished} unpublished outbox rows in arcadia_{database}")


@pytest.mark.parametrize("database", sorted(OUTBOXES))
def test_no_outbox_message_exhausted_its_retries(database: str):
    """A row past its attempt limit has stopped being retried and is waiting for a human."""
    table, _, exhausted_clause = OUTBOXES[database]
    if exhausted_clause is None:
        pytest.skip(f"arcadia_{database}.{table} does not count attempts")
    stuck = int(a.psql(database, f"SELECT count(*) FROM {table} WHERE {exhausted_clause}"))
    assert stuck == 0, f"{stuck} permanently failed outbox rows in arcadia_{database}"


def test_every_balance_equals_the_sum_of_its_ledger():
    """The wallet's central invariant, checked against real movements.

    The ledger is the source of truth and the balance column is a cached projection of it. If
    they disagree, money has been created or destroyed.
    """
    mismatches = a.psql(
        "wallet",
        """
        SELECT count(*) FROM (
            SELECT w.id
            FROM wallets w
            LEFT JOIN ledger_entries l ON l.wallet_id = w.id
            GROUP BY w.id, w.balance_minor
            HAVING w.balance_minor <> COALESCE(SUM(
                CASE WHEN l.direction = 'CREDIT' THEN l.amount_minor ELSE -l.amount_minor END
            ), 0)
        ) mismatched
        """,
    )
    assert mismatches == "0", f"{mismatches} wallet(s) disagree with their ledger"


def test_no_order_is_stuck_mid_saga():
    """A PENDING order that has stopped moving is a purchase in limbo.

    The sweeper re-issues the outstanding command and eventually abandons it, so any order
    still PENDING at the end of the suite means neither happened.
    """
    stuck = a.psql(
        "order",
        "SELECT count(*) FROM orders o JOIN saga_state s ON s.order_id = o.id "
        "WHERE o.state = 'PENDING' AND s.last_advanced_at < now() - interval '3 minutes'",
    )
    assert stuck == "0", f"{stuck} order(s) stuck in PENDING for over three minutes"


def test_no_saga_was_abandoned():
    """An abandoned saga is a person whose money and entitlement need reconciling by hand."""
    abandoned = a.psql(
        "order",
        "SELECT count(*) FROM saga_state WHERE status = 'FAILED' AND attempts >= 5",
    )
    assert abandoned == "0", f"{abandoned} saga(s) were abandoned after exhausting retries"


def test_every_completed_order_balances():
    """The 70/30 invariant, across every order the suite created.

    There is a CHECK constraint saying the same thing, so this failing would mean the
    constraint was dropped.
    """
    unbalanced = a.psql(
        "order",
        "SELECT count(*) FROM orders "
        "WHERE developer_share_minor + platform_share_minor <> total_charged_minor",
    )
    assert unbalanced == "0", f"{unbalanced} order(s) do not balance"


def test_no_entitlement_is_duplicated():
    """One entitlement per user per game — enforced by a unique constraint, checked here
    because a duplicate would mean the constraint is missing."""
    duplicates = a.psql(
        "catalog",
        "SELECT count(*) FROM (SELECT game_id, owner_id FROM ownerships "
        "GROUP BY game_id, owner_id HAVING count(*) > 1) d",
    )
    assert duplicates == "0", f"{duplicates} duplicated entitlement(s)"


def test_no_media_row_lacks_its_bytes():
    """A row whose file is gone is a broken download for something the catalogue lists.

    Asks the store where the bytes are rather than assuming a directory. This walked the
    media-data volume with `test -f` and passed for months; the day `STORAGE_BACKEND` became
    `s3` it reported every file on the platform as missing — accurate about the volume, wrong
    about the platform. `stored_object_keys` reads whichever backend is running, which also makes
    this the check that catches a backend switched without `make media-migrate`.
    """
    rows = a.psql("media", "SELECT object_key FROM media_objects WHERE deleted_at IS NULL")
    expected = {key for key in rows.splitlines() if key}

    missing = sorted(expected - a.stored_object_keys())
    assert not missing, (
        f"{len(missing)} metadata row(s) with no bytes behind them "
        f"in the {a.media_backend()} store: {missing[:10]}"
    )
