"""Refunds, requirement 1.4 and §6.2."""

from __future__ import annotations

import time

import arcadia as a
import pytest



@pytest.mark.slow
def test_a_refund_is_reported_as_refunding_not_refunded(completed_order, funded_buyer):
    """The commands are out; the money is not back yet.

    Saying REFUNDED here would overstate what has happened with somebody's money, which is
    why the state exists at all — the architecture document's diagram does not list it.
    """
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders/{completed_order['id']}/refund",
        user=funded_buyer, key=f"e2e-refund-{completed_order['id']}",
    )
    assert response.status == 200, response
    assert response.body["state"] == "REFUNDING"


@pytest.mark.slow
def test_the_order_becomes_refunded_once_the_wallet_confirms(completed_order, funded_buyer):
    deadline = time.monotonic() + 30
    order = None
    while time.monotonic() < deadline:
        order = a.call(
            f"GET", f"{a.ORDER}/v1/orders/{completed_order['id']}", user=funded_buyer
        ).body
        if order["state"] == "REFUNDED":
            break
        time.sleep(0.5)
    assert order["state"] == "REFUNDED", order
    assert order["refunded_at"] is not None


@pytest.mark.slow
def test_the_buyer_has_their_money_back(purchase, funded_buyer, balance):
    """Back to exactly what it was before the purchase, not to a fixed figure: the suite
    makes several purchases and an absolute total would couple this to test ordering."""
    assert balance(funded_buyer) == purchase["before"]["buyer"]


@pytest.mark.slow
def test_both_revenue_shares_were_reversed(purchase, developer, balance):
    """The reversals proceed independently of the buyer's refund, but with nothing spent they
    both land — so the developer is back to nothing and the platform to where it started."""
    assert balance(developer) == 0
    assert balance(a.PLATFORM_USER) == purchase["before"]["platform"]


@pytest.mark.slow
def test_the_game_left_the_library(completed_order, game, funded_buyer):
    response = a.call("GET", f"{a.CATALOG}/v1/library", user=funded_buyer)
    assert game["id"] not in [item["game_id"] for item in response.body["items"]]


@pytest.mark.slow
def test_a_second_refund_is_refused(completed_order, funded_buyer):
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders/{completed_order['id']}/refund",
        user=funded_buyer, key=f"e2e-refund-again-{completed_order['id']}",
    )
    assert response.status == 409
    assert response.body["reason"] == "ALREADY_REFUNDED"


@pytest.mark.slow
def test_someone_elses_order_cannot_be_refunded(completed_order, stranger):
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders/{completed_order['id']}/refund",
        user=stranger, key=f"e2e-refund-theft-{completed_order['id']}",
    )
    assert response.status == 403
    assert response.body["reason"] == "NOT_ORDER_BUYER"


@pytest.mark.slow
def test_a_refunded_game_can_be_bought_again(completed_order, game, funded_buyer, settled):
    """Re-granting reuses the entitlement row, so there is one history per (user, game)."""
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-rebuy-{game['id']}", body={"game_id": game["id"]},
    )
    assert response.status == 202, response
    order = settled(response.body["id"], funded_buyer)
    assert order["state"] == "COMPLETED"

    library = a.call("GET", f"{a.CATALOG}/v1/library", user=funded_buyer)
    owned = [item for item in library.body["items"] if item["game_id"] == game["id"]]
    assert len(owned) == 1, owned
