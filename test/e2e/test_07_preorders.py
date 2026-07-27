"""Pre-orders against the running platform. Requirement 1.5.

The flow that money is *reserved* rather than spent, and taken when the game ships. This is
the one flow that touches the wallet's hold mechanism, so it is also the only proof that
mechanism is wired up at all.
"""

from __future__ import annotations

import time

import arcadia as a
import pytest

from conftest import DEVELOPER_SHARE, PLATFORM_SHARE, PRICE


def a_priced_game(developer: str, support: str) -> str:
    """A game approved and priced but not published, ready for pre-orders."""
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": "Unreleased Drift", "description": "Coming soon."},
    )
    assert created.status == 201, created
    game_id = created.body["id"]

    for method, path, body, role, user in [
        ("POST", f"/v1/games/{game_id}/versions",
         {"version": "0.9.0", "file_ref": "preview", "size_bytes": 1024}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/submit", None, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/review/start", None, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/review/approve", {"note": "fine"}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/price", {"amount_minor": PRICE}, "DEVELOPER", developer),
    ]:
        step = a.call(method, f"{a.CATALOG}{path}", user=user, role=role, body=body)
        assert step.status in (200, 201), (path, step)

    return game_id


def wait_for_state(order_id: str, user: str, wanted: str, *, timeout: float = 45.0) -> dict:
    deadline = time.monotonic() + timeout
    order = None
    while time.monotonic() < deadline:
        response = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=user)
        assert response.status == 200, response
        order = response.body
        if order["state"] == wanted:
            return order
        time.sleep(0.5)
    raise AssertionError(f"order {order_id} never reached {wanted}; last seen {order}")


@pytest.fixture(scope="session")
def preorder_game(developer, support) -> str:
    game_id = a_priced_game(developer, support)
    response = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/preorders",
        user=developer, role="DEVELOPER",
        body={"release_at": "2027-01-01T00:00:00Z"},
    )
    assert response.status == 200, response
    assert response.body["state"] == "PREORDER"
    return game_id


# --- the catalog side ---------------------------------------------------


def test_a_preordered_game_reports_itself_as_such(preorder_game, funded_buyer):
    """The order service branches on this rather than on the catalog's state machine."""
    response = a.call(
        "GET", f"{a.CATALOG}/v1/games/{preorder_game}/saleability", user=funded_buyer
    )
    assert response.status == 200, response
    assert response.body["purchasable"] is True
    assert response.body["preorder"] is True
    assert response.body["release_at"] is not None


def test_a_published_game_is_not_a_preorder(game, funded_buyer):
    response = a.call("GET", f"{a.CATALOG}/v1/games/{game['id']}/saleability", user=funded_buyer)
    assert response.body["preorder"] is False


# --- reserving ----------------------------------------------------------


@pytest.mark.slow
def test_a_preorder_reserves_the_money_without_spending_it(
    preorder_game, funded_buyer, balance
):
    """The whole point. The balance drops — the funds are committed — but nothing was paid to
    anybody, and the wallet reports it as held rather than spent."""
    before = balance(funded_buyer)

    placed = a.call(
        "POST", f"{a.ORDER}/v1/preorders", user=funded_buyer,
        key=f"e2e-pre-{preorder_game}", body={"game_id": preorder_game},
    )
    assert placed.status == 202, placed
    order = wait_for_state(placed.body["id"], funded_buyer, "RESERVED")

    assert order["type"] == "PREORDER"
    assert order["cancellable"] is True
    # Nothing to refund yet, because nothing has been paid.
    assert order["refundable_until"] is None

    wallet = a.call("GET", f"{a.WALLET}/v1/wallets/me", user=funded_buyer).body
    assert int(wallet["held"]["amount_minor"]) >= PRICE, wallet
    assert int(wallet["available"]["amount_minor"]) == before - PRICE, wallet


@pytest.mark.slow
def test_nobody_has_been_paid_while_it_waits(preorder_game, developer, balance):
    """No ownership, no revenue split. There is nothing to grant and nothing to share."""
    library = a.call("GET", f"{a.CATALOG}/v1/library", user=developer)
    assert preorder_game not in [i["game_id"] for i in library.body["items"]]


@pytest.mark.slow
def test_buying_a_preorder_game_outright_is_refused(preorder_game, funded_buyer):
    """A buyer who pressed "buy" should not have funds held for weeks without expecting it."""
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-pre-wrong-{preorder_game}", body={"game_id": preorder_game},
    )
    assert response.status == 422
    assert response.body["reason"] == "GAME_IS_PREORDER_ONLY"


@pytest.mark.slow
def test_a_preorder_cannot_be_sent_as_a_gift(preorder_game, funded_buyer, friend):
    response = a.call(
        "POST", f"{a.ORDER}/v1/gifts", user=funded_buyer,
        key=f"e2e-pre-gift-{preorder_game}",
        body={"game_id": preorder_game, "recipient_id": friend},
    )
    assert response.status == 422


# --- release ------------------------------------------------------------


@pytest.mark.slow
def test_releasing_the_game_turns_the_reservation_into_a_purchase(
    preorder_game, funded_buyer, developer, balance
):
    """The moment a pre-order becomes a sale: the hold is captured, the game is granted, and
    the developer is paid."""
    order_id = a.call(
        "POST", f"{a.ORDER}/v1/preorders", user=funded_buyer,
        key=f"e2e-pre-{preorder_game}", body={"game_id": preorder_game},
    ).body["id"]
    wait_for_state(order_id, funded_buyer, "RESERVED")

    developer_before = balance(developer)
    platform_before = balance(a.PLATFORM_USER)

    released = a.call(
        "POST", f"{a.CATALOG}/v1/games/{preorder_game}/release", user=developer, role="DEVELOPER"
    )
    assert released.status == 200, released
    assert released.body["state"] == "PUBLISHED"

    order = wait_for_state(order_id, funded_buyer, "COMPLETED")
    assert order["completed_at"] is not None
    # Now it is a real purchase, so the twelve-hour window applies.
    assert order["refundable_until"] is not None
    assert order["cancellable"] is False

    assert balance(developer) - developer_before == DEVELOPER_SHARE
    assert balance(a.PLATFORM_USER) - platform_before == PLATFORM_SHARE

    library = a.call("GET", f"{a.CATALOG}/v1/library", user=funded_buyer)
    assert preorder_game in [i["game_id"] for i in library.body["items"]]


@pytest.mark.slow
def test_the_hold_is_gone_once_captured(preorder_game, funded_buyer):
    wallet = a.call("GET", f"{a.WALLET}/v1/wallets/me", user=funded_buyer).body
    assert int(wallet["held"]["amount_minor"]) == 0, wallet


# --- cancelling ---------------------------------------------------------


@pytest.mark.slow
def test_a_buyer_can_cancel_and_get_the_reservation_back(developer, support, balance, stranger, admin):
    """Released, not refunded — nothing was ever taken.

    Uses its own buyer and game so it does not disturb the release flow above.
    """
    buyer = a.new_id()
    a.provision_wallet(buyer)
    funded = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-cancel-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed for the cancellation test",
        },
    )
    assert funded.status == 200, funded
    before = balance(buyer)

    game_id = a_priced_game(developer, support)
    a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/preorders", user=developer, role="DEVELOPER",
        body={"release_at": "2027-06-01T00:00:00Z"},
    )

    order_id = a.call(
        "POST", f"{a.ORDER}/v1/preorders", user=buyer,
        key=f"e2e-pre-cancel-{game_id}", body={"game_id": game_id},
    ).body["id"]
    wait_for_state(order_id, buyer, "RESERVED")

    cancelled = a.call("POST", f"{a.ORDER}/v1/orders/{order_id}/cancel", user=buyer)
    assert cancelled.status == 200, cancelled
    assert cancelled.body["state"] == "CANCELLED"

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        wallet = a.call("GET", f"{a.WALLET}/v1/wallets/me", user=buyer).body
        if int(wallet["held"]["amount_minor"]) == 0:
            break
        time.sleep(0.5)

    assert int(wallet["held"]["amount_minor"]) == 0, wallet
    assert balance(buyer) == before


@pytest.mark.slow
def test_a_cancelled_release_gives_every_reservation_back(developer, support, admin, balance):
    """The buyer committed money to a game that will not arrive."""
    buyer = a.new_id()
    a.provision_wallet(buyer)
    a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-withdraw-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed for the cancelled-release test",
        },
    )
    before = balance(buyer)

    game_id = a_priced_game(developer, support)
    a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/preorders", user=developer, role="DEVELOPER",
        body={"release_at": "2027-09-01T00:00:00Z"},
    )
    order_id = a.call(
        "POST", f"{a.ORDER}/v1/preorders", user=buyer,
        key=f"e2e-pre-cancelled-release-{game_id}", body={"game_id": game_id},
    ).body["id"]
    wait_for_state(order_id, buyer, "RESERVED")

    withdrawn = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/withdraw", user=developer, role="DEVELOPER",
        body={"reason": "the release was cancelled"},
    )
    assert withdrawn.status == 200, withdrawn

    order = wait_for_state(order_id, buyer, "CANCELLED")
    assert order["failure_reason"] == "RELEASE_CANCELLED"
    assert balance(buyer) == before
