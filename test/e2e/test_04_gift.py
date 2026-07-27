"""Gifts, requirement 1.4."""

from __future__ import annotations

import arcadia as a
import pytest

from conftest import GIFT_MESSAGE_FEE as MESSAGE_FEE
from conftest import PRICE


@pytest.mark.slow
def test_a_message_costs_two_percent_extra(gift_order):
    assert gift_order["base_price"]["amount_minor"] == str(PRICE)
    assert gift_order["gift"]["message_fee"]["amount_minor"] == str(MESSAGE_FEE)
    assert gift_order["total_charged"]["amount_minor"] == str(PRICE + MESSAGE_FEE)


@pytest.mark.slow
def test_the_buyer_pays_and_the_recipient_receives(gift_order, funded_buyer, friend):
    assert gift_order["buyer_id"] == funded_buyer
    assert gift_order["gift"]["recipient_id"] == friend

    library = a.call("GET", f"{a.CATALOG}/v1/library", user=friend)
    owned = [i for i in library.body["items"] if i["game_id"] == gift_order["game_id"]]
    assert len(owned) == 1
    assert owned[0]["gifted_by"] == funded_buyer


@pytest.mark.slow
def test_the_message_fee_goes_to_the_platform_not_the_developer(gift_order):
    """A developer's income should not change because a buyer attached a note."""
    assert gift_order["developer_share"]["amount_minor"] == "700000"
    assert gift_order["platform_share"]["amount_minor"] == str(300_000 + MESSAGE_FEE)


@pytest.mark.slow
def test_a_gift_cannot_be_refunded(gift_order, funded_buyer):
    """Requirement 1.4. The game is in somebody else's library, and taking it back would
    punish them for the buyer's change of mind."""
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders/{gift_order['id']}/refund",
        user=funded_buyer, key=f"e2e-refund-gift-{gift_order['id']}",
    )
    assert response.status == 422
    assert response.body["reason"] == "GIFT_NOT_REFUNDABLE"


@pytest.mark.slow
def test_a_gift_has_no_refund_deadline(gift_order):
    assert gift_order["refundable_until"] is None


def test_a_message_over_five_hundred_words_is_refused(game, funded_buyer, friend):
    response = a.call(
        "POST", f"{a.ORDER}/v1/gifts", user=funded_buyer, key=f"e2e-gift-long-{a.new_id()}",
        body={"game_id": game["id"], "recipient_id": friend, "message": "word " * 501},
    )
    assert response.status == 400
    assert response.body["reason"] == "GIFT_MESSAGE_TOO_LONG"


def test_a_gift_to_yourself_is_refused(game, funded_buyer):
    response = a.call(
        "POST", f"{a.ORDER}/v1/gifts", user=funded_buyer, key=f"e2e-self-gift-{a.new_id()}",
        body={"game_id": game["id"], "recipient_id": funded_buyer},
    )
    assert response.status == 400
    assert response.body["reason"] == "CANNOT_GIFT_TO_SELF"
