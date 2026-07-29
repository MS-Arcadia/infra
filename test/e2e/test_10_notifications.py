"""Notifications, against the live platform. Requirement 1.10.

Everything in the notification service's own suite runs against a fake store and a payload copied
from another service's source. That proves the translation is right *if* the payload is what it says.
This file is the part that cannot be faked: an event published by a different service, in a different
language in two cases, crossing a real Kafka topic, and arriving as a row a person can read over HTTP.

The two cases worth the wall-clock are here for a reason. A **gift** is the only event on the platform
that notifies two different people from one message, so it is where a fan-out collapsing into a single
row would show. An **instalment default** is the only place an entitlement is taken away without a
refund — the notification is the sole thing that tells the buyer why their game disappeared, so it not
arriving is the most consequential silent failure this service has.
"""

from __future__ import annotations

import time

import arcadia as a
import pytest


def inbox(user: str, *, unread_only: bool = False) -> list[dict]:
    query = "?limit=100&unread_only=true" if unread_only else "?limit=100"
    response = a.call("GET", f"{a.NOTIFICATION}/v1/notifications{query}", user=user)
    assert response.status == 200, response
    return response.body["items"]


def wait_for(user: str, kind: str, *, subject_id: str | None = None, timeout: float = 45.0) -> dict:
    """Poll until a notification of this kind arrives, and return it.

    Polling rather than sleeping: the path is publish → outbox dispatcher → Kafka → consumer →
    insert, which is normally a second or two and occasionally not. A fixed wait would be either
    slow or flaky depending on the machine.
    """
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        items = inbox(user)
        seen = [item["kind"] for item in items]
        for item in items:
            if item["kind"] == kind and (subject_id is None or item["subject_id"] == subject_id):
                return item
        time.sleep(0.5)
    raise AssertionError(
        f"{kind} never arrived for {user} within {timeout}s"
        + (f" for subject {subject_id}" if subject_id else "")
        + f".\nWhat did arrive: {seen or 'nothing'}.\n"
        f"Check `docker logs arcadia-notification` and whether the producing service's outbox "
        f"drained: the event has to cross Kafka to get here."
    )


# --- a purchase ----------------------------------------------------------


def test_a_completed_purchase_tells_the_buyer(purchase, funded_buyer, game):
    """The simplest end of the contract: order-service publishes, this service tells somebody."""
    told = wait_for(funded_buyer, "PURCHASE_COMPLETED", subject_id=purchase["order"]["id"])

    assert game["title"] in told["title"]
    assert told["subject_type"] == "ORDER"
    assert told["read"] is False


def test_the_developer_is_told_the_review_decision(game, developer):
    """Catalog's `GameApproved`, and the case that proves the audience is not the actor: Support
    approved it, and the *developer* is the one told."""
    told = wait_for(developer, "GAME_APPROVED", subject_id=game["id"])

    assert game["title"] in told["title"]
    assert told["subject_type"] == "GAME"


def test_support_is_not_told_about_the_game_they_approved(game, support):
    """The other half of the same claim. If this service notified whoever acted, Support would
    collect a notification for every game they ever reviewed."""
    kinds = [item["kind"] for item in inbox(support)]
    assert "GAME_APPROVED" not in kinds, f"support was told about their own approval: {kinds}"


# --- a gift: one event, two people ---------------------------------------


def test_the_gift_recipient_is_told_and_gets_the_message(gift_order, friend):
    told = wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])

    assert "gift" in told["title"].lower()
    # The sender's own words are the body, whole. Text this service wrote must not displace them.
    assert told["body"] == "happy birthday, enjoy the game"


def test_the_sender_is_told_it_landed(gift_order, funded_buyer):
    told = wait_for(funded_buyer, "PURCHASE_COMPLETED", subject_id=gift_order["id"])
    assert "gift" in told["title"].lower()


def test_one_gift_event_produced_exactly_two_rows(gift_order, friend, funded_buyer):
    """The reason the unique constraint is `(event_id, user_id)` and not `event_id`.

    Read from the database because it is the only place the identity of the causing event is
    visible. A unique index on `event_id` alone would have stored the recipient's notification and
    silently never told the buyer — and the API cannot show that, because each person's own view
    would look correct.
    """
    wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])
    wait_for(funded_buyer, "PURCHASE_COMPLETED", subject_id=gift_order["id"])

    rows = a.psql(
        "notification",
        "SELECT event_id, count(*), count(DISTINCT user_id) FROM notifications "
        f"WHERE subject_id = '{gift_order['id']}' AND kind IN ('GIFT_RECEIVED', "
        "'PURCHASE_COMPLETED') GROUP BY event_id",
    ).strip()

    assert rows, f"no notification rows for gift order {gift_order['id']}"
    for line in rows.splitlines():
        event_id, total, people = (part.strip() for part in line.split("|"))
        assert total == people, (
            f"event {event_id} produced {total} rows for {people} distinct people — "
            f"a redelivery was recorded twice for somebody"
        )


# --- reading, over HTTP --------------------------------------------------


def test_the_unread_count_agrees_with_the_list(gift_order, friend):
    wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])

    unread = inbox(friend, unread_only=True)
    response = a.call("GET", f"{a.NOTIFICATION}/v1/notifications/unread-count", user=friend)
    assert response.status == 200, response
    assert response.body["unread"] == len(unread)


def test_marking_one_read_lowers_the_count(gift_order, friend):
    told = wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])
    before = a.call(
        "GET", f"{a.NOTIFICATION}/v1/notifications/unread-count", user=friend
    ).body["unread"]

    marked = a.call("POST", f"{a.NOTIFICATION}/v1/notifications/{told['id']}/read", user=friend)
    assert marked.status == 200, marked
    assert marked.body["read"] is True
    assert marked.body["read_at"]

    # Idempotent: a client that marks rows read as it renders them sends this on every refresh.
    again = a.call("POST", f"{a.NOTIFICATION}/v1/notifications/{told['id']}/read", user=friend)
    assert again.status == 200, again
    assert again.body["read_at"] == marked.body["read_at"]

    after = a.call(
        "GET", f"{a.NOTIFICATION}/v1/notifications/unread-count", user=friend
    ).body["unread"]
    assert after == before - 1


def test_nobody_can_read_somebody_elses(gift_order, friend, stranger):
    """The subject comes from the token and from nothing else — not from a query parameter, and not
    for staff either.

    Asserted as "the friend's row is absent" rather than "the list is empty": the stranger has
    notifications of their own by the time the whole suite has run, and asserting an empty list made
    this pass on its own and fail in the suite. An empty answer would also be the weaker check — a
    service that ignored the parameter and one that returned nothing at all look the same.
    """
    theirs = wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])

    for role in ("BASIC_USER", "ADMIN", "SUPPORT"):
        response = a.call(
            "GET", f"{a.NOTIFICATION}/v1/notifications?user_id={friend}", user=stranger, role=role
        )
        assert response.status == 200, response
        ids = [item["id"] for item in response.body["items"]]
        assert theirs["id"] not in ids, f"a {role} read another user's notification"
        # And every row that did come back belongs to the caller: the gift's subject is the friend's
        # order, so nothing about it should appear here at all.
        subjects = [item["subject_id"] for item in response.body["items"]]
        assert gift_order["id"] not in subjects, f"a {role} saw another user's order"


def test_marking_somebody_elses_read_is_not_found(gift_order, friend, stranger):
    """404 rather than 403: "forbidden" confirms the id is real, and a title says what happened to
    whom."""
    told = wait_for(friend, "GIFT_RECEIVED", subject_id=gift_order["id"])

    response = a.call(
        "POST", f"{a.NOTIFICATION}/v1/notifications/{told['id']}/read", user=stranger
    )
    assert response.status == 404, response


def test_a_notification_cannot_be_created_over_http(admin):
    """Requirement 1.10 is event-driven. A notification nobody can inject is one a user can trust —
    and an admin is the strongest case to make it with."""
    response = a.call(
        "POST",
        f"{a.NOTIFICATION}/v1/notifications",
        user=admin,
        role="ADMIN",
        body={"user_id": admin, "title": "you have won a prize", "body": "click here"},
    )
    assert response.status == 405, response


# --- the one that matters most -------------------------------------------


@pytest.mark.slow
def test_a_defaulted_plan_tells_the_buyer_their_game_was_taken(developer, support, admin, balance):
    """The only place on the platform where an entitlement is removed without a refund.

    Requirement 1.10 lists it explicitly, and it is the notification whose absence would be worst:
    the game vanishes from the library either way, and this row is the only thing that says why.

    Driven exactly as `test_09_instalments.py` drives it — the plan's own helpers, so the event
    published here is the real one rather than a shape this file invented.
    """
    from test_09_instalments import (
        a_published_game,
        collect_until,
        place,
        plan_of,
        wait_for_state,
    )

    buyer = a.new_id()
    a.provision_wallet(buyer)
    funded = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-notify-default-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed for the notification default test",
        },
    )
    assert funded.status in (200, 201), funded

    game_id = a_published_game(developer, support)
    placed = place(buyer, game_id, instalments=3, interval_days=1)
    order_id = placed.body["id"]
    wait_for_state(order_id, buyer, "PAYING")

    # The plan is live and the game is in the library, so the buyer should already know.
    #
    # `INSTALMENT_PLAN_STARTED`, not `PURCHASE_COMPLETED` or `INSTALMENT_PAID` — writing this test
    # is what showed neither of those is published here. An instalment order stays in PAYING until
    # the last payment and `_publish_completed` is guarded on COMPLETED, while the first payment is
    # recorded inside the purchase saga rather than through the collection routine that publishes
    # `InstalmentPaid`. `InstalmentPlanStarted` was the only event on the day the buyer got the
    # game, and until this suite went looking, nothing consumed it.
    started = wait_for(buyer, "INSTALMENT_PLAN_STARTED")
    assert "library" in started["body"], started

    plan_id = plan_of(order_id, buyer)["id"]
    paid_before = balance(buyer)

    # Push the next payment past its grace period, then empty the wallet so a debit could not
    # succeed even if one were attempted. The alternative is waiting a week.
    a.psql(
        "order",
        f"UPDATE instalments SET due_at = now() - interval '60 days' "
        f"WHERE state = 'DUE' AND plan_id = '{plan_id}'",
    )
    a.psql(
        "order",
        f"UPDATE instalment_plans SET next_due_at = now() - interval '60 days' "
        f"WHERE id = '{plan_id}'",
    )
    a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-drain-notify-default-{buyer}",
        body={
            "direction": "DEBIT",
            "amount": {"amount_minor": str(paid_before), "currency": "IRR"},
            "reason": "drained so the plan cannot be collected",
        },
    )

    order = collect_until(order_id, buyer, admin, "DEFAULTED")
    assert order["state"] == "DEFAULTED"

    told = wait_for(buyer, "INSTALMENT_PLAN_DEFAULTED")
    # It has to say the game was removed, not merely that a payment failed. "Your payment did not
    # go through" would leave somebody looking for a game that is no longer there.
    assert "removed" in told["title"].lower() or "removed" in told["body"].lower(), told
    assert told["subject_type"] in ("ORDER", "INSTALMENT_PLAN"), told
