"""The purchase saga, requirement 1.4 and §6.1.

Three services, six commands, four replies — over real Kafka, against real Postgres.
"""

from __future__ import annotations

import arcadia as a
import pytest

from conftest import DEVELOPER_SHARE, PLATFORM_SHARE, PRICE


def test_a_quote_charges_nothing_and_shows_the_split(game, funded_buyer, balance):
    before = balance(funded_buyer)
    response = a.call("GET", f"{a.ORDER}/v1/quotes/{game['id']}", user=funded_buyer)

    assert response.status == 200
    assert response.body["purchasable"] is True
    assert response.body["base_price"]["amount_minor"] == str(PRICE)
    assert response.body["developer_share"]["amount_minor"] == str(DEVELOPER_SHARE)
    assert response.body["platform_share"]["amount_minor"] == str(PLATFORM_SHARE)
    assert balance(funded_buyer) == before


def test_a_quote_for_a_gift_with_a_message_includes_the_two_percent(game, funded_buyer, friend):
    response = a.call(
        "GET",
        f"{a.ORDER}/v1/quotes/{game['id']}?recipient_id={friend}&with_message=true",
        user=funded_buyer,
        role="SUPPORT",
    )
    assert response.status == 200
    assert response.body["message_fee"]["amount_minor"] == "20000"
    assert response.body["total_charged"]["amount_minor"] == "1020000"


@pytest.mark.slow
def test_the_saga_completes(completed_order):
    assert completed_order["completed_at"] is not None
    assert completed_order["refundable_until"] is not None


@pytest.mark.slow
def test_the_buyer_was_charged_exactly_the_price(purchase, funded_buyer, balance):
    assert purchase["order"]["total_charged"]["amount_minor"] == str(PRICE)
    assert purchase["before"]["buyer"] - balance(funded_buyer) == PRICE


@pytest.mark.slow
def test_the_developer_received_seventy_percent(purchase, developer, balance):
    """The developer's wallet is new and this is its first sale, so the balance *is* the
    share."""
    assert balance(developer) == DEVELOPER_SHARE


@pytest.mark.slow
def test_the_platform_received_thirty_percent(purchase, balance):
    assert balance(a.PLATFORM_USER) - purchase["before"]["platform"] == PLATFORM_SHARE


@pytest.mark.slow
def test_the_shares_add_up_to_what_the_buyer_paid(completed_order):
    """The invariant the order service exists to protect, checked on a real order."""
    charged = int(completed_order["total_charged"]["amount_minor"])
    developer = int(completed_order["developer_share"]["amount_minor"])
    platform = int(completed_order["platform_share"]["amount_minor"])
    assert developer + platform == charged


@pytest.mark.slow
def test_the_game_is_in_the_buyers_library(completed_order, game, funded_buyer):
    response = a.call("GET", f"{a.CATALOG}/v1/library", user=funded_buyer)
    assert response.status == 200
    assert game["id"] in [item["game_id"] for item in response.body["items"]]


@pytest.mark.slow
def test_the_same_game_cannot_be_bought_twice(completed_order, game, funded_buyer):
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-second-{game['id']}", body={"game_id": game["id"]},
    )
    assert response.status == 409
    assert response.body["reason"] == "GAME_ALREADY_OWNED"


@pytest.mark.slow
def test_the_original_key_replays_rather_than_reporting_a_conflict(
    completed_order, game, funded_buyer
):
    """A retry of a request that succeeded must get its order back, not a 409.

    This was a real bug: the catalog was asked before the idempotency store, so once the
    buyer owned the game a retry was answered GAME_ALREADY_OWNED.
    """
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-buy-{game['id']}", body={"game_id": game["id"]},
    )
    assert response.status == 202
    assert response.body["id"] == completed_order["id"]
    assert response.body["idempotent_replay"] is True


@pytest.mark.slow
def test_the_same_key_with_a_different_body_is_refused(completed_order, game, funded_buyer):
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-buy-{game['id']}", body={"game_id": a.new_id()},
    )
    assert response.status == 409
    assert response.body["reason"] == "IDEMPOTENCY_KEY_REUSED"


def test_a_purchase_without_an_idempotency_key_is_refused(game, funded_buyer):
    """Mandatory, never defaulted: a generated key would make every retry look new."""
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer, body={"game_id": game["id"]}
    )
    assert response.status == 400
    assert response.body["reason"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.slow
def test_a_buyer_with_no_money_fails_without_compensation(game, wallets, settled, stranger):
    """The stranger has a wallet but no balance.

    Nothing was granted and nothing was charged, so there is nothing to undo — and a refund
    here would credit a wallet that was never debited.
    """
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=stranger,
        key=f"e2e-broke-{game['id']}", body={"game_id": game["id"]},
    )
    assert response.status == 202

    order = settled(response.body["id"], stranger)
    assert order["state"] == "FAILED"
    assert order["failure_reason"] == "INSUFFICIENT_FUNDS"

    library = a.call("GET", f"{a.CATALOG}/v1/library", user=stranger)
    assert game["id"] not in [item["game_id"] for item in library.body["items"]]
