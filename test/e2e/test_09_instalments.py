"""Buying a game in instalments, against the live platform. Requirement 3.3.

The interesting checks here are the ones a unit test cannot make: that the wallet really is
debited only part of the price, that the developer really is credited per payment, and that a
plan which runs out of grace really does take the game out of the buyer's library.

Every plan here uses a one-day interval, and the platform's clock is real, so nothing can wait
for a payment to fall due naturally. The collection endpoint is driven directly instead — which
is what it is for.
"""

from __future__ import annotations

import arcadia as a
import pytest

from conftest import PRICE


@pytest.fixture
def plan_buyer(admin, wallets) -> str:
    """A funded buyer of their own, so the totals in each test are unambiguous.

    `wallets` is depended on for the *developer's* sake, not the buyer's: the revenue split
    credits the developer, and a credit to a user with no wallet is dead-lettered. Without this
    the first run of this file left an order stuck at SPLIT — which is exactly the failure, and
    exactly the confusion, that not declaring the dependency causes.
    """
    buyer = a.new_id()
    a.provision_wallet(buyer)
    response = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-plan-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed for an instalment test",
        },
    )
    assert response.status in (200, 201), response
    return buyer


def a_published_game(developer, support) -> str:
    from test_07_preorders import a_priced_game

    game_id = a_priced_game(developer, support)
    published = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/publish", user=support, role="SUPPORT"
    )
    assert published.status in (200, 201), published
    return game_id


def place(buyer: str, game_id: str, *, instalments: int = 4, interval_days: int = 1):
    return a.call(
        "POST", f"{a.ORDER}/v1/instalment-orders", user=buyer,
        key=f"e2e-instalment-{game_id}-{buyer}",
        body={
            "game_id": game_id,
            "instalments": instalments,
            "interval_days": interval_days,
        },
    )


def plan_of(order_id: str, buyer: str) -> dict:
    response = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}/instalment-plan", user=buyer)
    assert response.status == 200, response
    return response.body


def wait_for_state(order_id: str, buyer: str, *states: str, tries: int = 60) -> dict:
    import time

    for _ in range(tries):
        order = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=buyer).body
        if order["state"] in states:
            return order
        time.sleep(0.25)
    raise AssertionError(f"order {order_id} stayed {order['state']}, wanted one of {states}")


def collect_until(order_id: str, buyer: str, admin: str, *states: str, tries: int = 30) -> dict:
    """Drive collection until the order reaches one of `states`.

    Real time, real clock: nothing here can wait for a payment to fall due naturally, and a
    payment in flight is not offered again — so each pass needs the previous one's reply to have
    landed before it does anything.
    """
    import time

    for _ in range(tries):
        a.call("POST", f"{a.ORDER}/v1/admin/instalments/collect", user=admin, role="ADMIN")
        time.sleep(0.4)
        order = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=buyer).body
        if order["state"] in states:
            return order
    raise AssertionError(f"order {order_id} stayed {order['state']}, wanted one of {states}")


@pytest.mark.slow
def test_only_the_first_payment_leaves_the_wallet(developer, support, plan_buyer, balance):
    """The whole point of the feature, and the thing a unit test cannot prove: the money really
    stays in the buyer's wallet."""
    game_id = a_published_game(developer, support)
    before = balance(plan_buyer)

    placed = place(plan_buyer, game_id, instalments=4)
    assert placed.status == 202, placed
    order = wait_for_state(placed.body["id"], plan_buyer, "PAYING")

    assert order["type"] == "INSTALMENT"
    # A quarter of the price, not the price.
    assert before - balance(plan_buyer) == PRICE // 4
    assert order["total_charged"]["amount_minor"] == str(PRICE)


@pytest.mark.slow
def test_the_game_is_playable_after_the_first_payment(developer, support, plan_buyer):
    """Not after the last. Withholding it would be layaway, not instalment credit."""
    game_id = a_published_game(developer, support)
    placed = place(plan_buyer, game_id)
    wait_for_state(placed.body["id"], plan_buyer, "PAYING")

    library = a.call("GET", f"{a.CATALOG}/v1/library", user=plan_buyer).body
    owned = [item["game_id"] for item in library["items"]]
    assert game_id in owned


@pytest.mark.slow
def test_the_developer_is_paid_per_payment_not_up_front(
    developer, support, plan_buyer, balance
):
    """Paying out 70% of a price the platform has not collected would mean lending the developer
    money and carrying the buyer's default risk on the platform's own books."""
    game_id = a_published_game(developer, support)
    developer_before = balance(developer)

    placed = place(plan_buyer, game_id, instalments=4)
    wait_for_state(placed.body["id"], plan_buyer, "PAYING")

    # 70% of one quarter, not 70% of the price.
    assert balance(developer) - developer_before == (PRICE // 4) * 70 // 100


@pytest.mark.slow
def test_the_schedule_is_visible_to_the_buyer(developer, support, plan_buyer):
    """A plan you cannot see is one you find out about when a payment is taken."""
    game_id = a_published_game(developer, support)
    placed = place(plan_buyer, game_id, instalments=4)
    wait_for_state(placed.body["id"], plan_buyer, "PAYING")

    plan = plan_of(placed.body["id"], plan_buyer)
    assert plan["state"] == "ACTIVE"
    assert len(plan["instalments"]) == 4
    assert plan["instalments"][0]["state"] == "PAID"
    assert plan["paid"]["amount_minor"] == str(PRICE // 4)
    assert plan["outstanding"]["amount_minor"] == str(PRICE - PRICE // 4)
    # The field that matters most: when the game gets taken back if they stop paying.
    assert plan["defaults_at"]


@pytest.mark.slow
def test_a_stranger_cannot_read_somebody_elses_plan(developer, support, plan_buyer):
    """A schedule says what somebody owes and when. Not less private than the sale."""
    game_id = a_published_game(developer, support)
    placed = place(plan_buyer, game_id)
    wait_for_state(placed.body["id"], plan_buyer, "PAYING")

    response = a.call(
        "GET", f"{a.ORDER}/v1/orders/{placed.body['id']}/instalment-plan", user=a.new_id()
    )
    assert response.status == 404, response


@pytest.mark.slow
def test_the_payments_collected_add_up_to_the_price(
    developer, support, plan_buyer, balance, admin
):
    """The number that has to be exactly right: over the whole plan the buyer is charged the
    price of the game, not a unit more or less."""
    game_id = a_published_game(developer, support)
    before = balance(plan_buyer)

    placed = place(plan_buyer, game_id, instalments=3, interval_days=1)
    order_id = placed.body["id"]
    wait_for_state(order_id, plan_buyer, "PAYING")

    # Nothing is due yet, so collection is a no-op — which is itself worth asserting.
    a.call("POST", f"{a.ORDER}/v1/admin/instalments/collect", user=admin, role="ADMIN")
    assert plan_of(order_id, plan_buyer)["paid"]["amount_minor"] == str(PRICE // 3 + PRICE % 3)

    # Then bring the remaining payments forward and collect them.
    a.psql(
        "order",
        f"UPDATE instalments SET due_at = now() - interval '1 day' "
        f"WHERE state = 'DUE' AND plan_id = '{plan_of(order_id, plan_buyer)['id']}'",
    )
    a.psql(
        "order",
        f"UPDATE instalment_plans SET next_due_at = now() - interval '1 day' "
        f"WHERE id = '{plan_of(order_id, plan_buyer)['id']}'",
    )
    # One pass per payment, waiting in between. A payment already in flight is deliberately not
    # offered again, so hammering the endpoint without waiting for the wallet's reply collects
    # exactly one payment and then nothing — which is the correct behaviour and made the first
    # version of this test look like a stuck plan.
    order = collect_until(order_id, plan_buyer, admin, "COMPLETED")
    assert order["state"] == "COMPLETED"
    assert before - balance(plan_buyer) == PRICE
    assert plan_of(order_id, plan_buyer)["outstanding"]["amount_minor"] == "0"


@pytest.mark.slow
def test_paying_off_early_settles_the_whole_price(developer, support, plan_buyer, balance):
    """Not asked for by the requirement, but a plan you cannot pay off early is a worse product
    than one you can — and it moves the same money in the same proportions."""
    game_id = a_published_game(developer, support)
    before = balance(plan_buyer)

    placed = place(plan_buyer, game_id, instalments=4)
    order_id = placed.body["id"]
    wait_for_state(order_id, plan_buyer, "PAYING")

    response = a.call("POST", f"{a.ORDER}/v1/orders/{order_id}/pay-off", user=plan_buyer)
    assert response.status == 200, response

    wait_for_state(order_id, plan_buyer, "COMPLETED")
    assert before - balance(plan_buyer) == PRICE


@pytest.mark.slow
def test_a_plan_that_runs_out_of_grace_loses_the_game(
    developer, support, plan_buyer, admin, balance
):
    """The only place on the platform where an entitlement is taken away without a refund.

    Proven end to end because it crosses three services: the plan defaults here, the catalog
    revokes the entitlement, and the wallet is asked for nothing at all.
    """
    game_id = a_published_game(developer, support)
    placed = place(plan_buyer, game_id, instalments=3, interval_days=1)
    order_id = placed.body["id"]
    wait_for_state(order_id, plan_buyer, "PAYING")

    plan_id = plan_of(order_id, plan_buyer)["id"]
    paid_before = balance(plan_buyer)

    # Push the next payment past its grace period. The alternative is waiting a week.
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
    # And empty the wallet, so a debit could not succeed even if one were attempted.
    a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{plan_buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-drain-{plan_buyer}",
        body={
            "direction": "DEBIT",
            "amount": {"amount_minor": str(paid_before), "currency": "IRR"},
            "reason": "drained so the plan cannot be collected",
        },
    )

    order = collect_until(order_id, plan_buyer, admin, "DEFAULTED")
    assert order["state"] == "DEFAULTED"

    plan = plan_of(order_id, plan_buyer)
    assert plan["state"] == "DEFAULTED"
    # What was already paid stays paid. The developer keeps their share of the months the
    # buyer did have the game.
    assert plan["paid"]["amount_minor"] != "0"

    # And the game is gone from the library.
    import time

    for _ in range(40):
        library = a.call("GET", f"{a.CATALOG}/v1/library", user=plan_buyer).body
        owned = [item["game_id"] for item in library["items"]]
        if game_id not in owned:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the game was still in the library after the plan defaulted")


@pytest.mark.slow
def test_a_refund_returns_only_what_was_collected(developer, support, plan_buyer, balance):
    """Refunding the price would pay the buyer money the platform never took."""
    game_id = a_published_game(developer, support)
    before = balance(plan_buyer)

    placed = place(plan_buyer, game_id, instalments=4)
    order_id = placed.body["id"]
    wait_for_state(order_id, plan_buyer, "PAYING")

    refund = a.call(
        "POST", f"{a.ORDER}/v1/orders/{order_id}/refund", user=plan_buyer,
        key=f"e2e-refund-plan-{order_id}",
    )
    assert refund.status == 200, refund
    wait_for_state(order_id, plan_buyer, "REFUNDED")

    # Back where they started, not up by three quarters of a game.
    assert balance(plan_buyer) == before
    assert plan_of(order_id, plan_buyer)["state"] == "CANCELLED"


@pytest.mark.slow
def test_a_discount_code_is_refused_on_a_plan(developer, support, plan_buyer):
    """`extra="forbid"` on the request, so this is a 422 from the edge — the field does not
    exist on an instalment order at all."""
    game_id = a_published_game(developer, support)
    response = a.call(
        "POST", f"{a.ORDER}/v1/instalment-orders", user=plan_buyer,
        key=f"e2e-plan-discount-{game_id}",
        body={"game_id": game_id, "instalments": 3, "discount_code": "ANY"},
    )
    # 400, not 422: this service reports a malformed body as INVALID_ARGUMENT. The field does
    # not exist on an instalment order at all, so `extra="forbid"` refuses it at the edge.
    assert response.status == 400, response
    assert "discount_code" in str(response.body)


@pytest.mark.slow
def test_one_payment_is_not_a_plan(developer, support, plan_buyer):
    game_id = a_published_game(developer, support)
    response = a.call(
        "POST", f"{a.ORDER}/v1/instalment-orders", user=plan_buyer,
        key=f"e2e-plan-one-{game_id}",
        body={"game_id": game_id, "instalments": 1},
    )
    assert response.status == 400, response
