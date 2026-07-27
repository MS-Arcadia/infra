"""A discount code applied to a real purchase.

§3.2 makes a code a reward — for writing a review, for a login streak. Two things go wrong with
one: it is consumed for nothing, or it is not consumed and becomes an unlimited discount. The
second is a money leak, so the design prefers the first — and these checks prove which happens.
"""

from __future__ import annotations

import arcadia as a
import pytest

from conftest import PRICE

DISCOUNT_BPS = 2000  # 20%
DISCOUNT = PRICE * DISCOUNT_BPS // 10_000


@pytest.fixture(scope="session")
def code(support) -> str:
    """A discount code, issued by Support as the requirements describe."""
    response = a.call(
        "POST", f"{a.WALLET}/v1/discount-codes", user=support, role="SUPPORT",
        key=f"e2e-code-{a.new_id()}",
        body={
            # The wallet's own field names — `percent_bps`, not a percentage. Read from
            # issueDiscountRequest rather than guessed: the first version of this test guessed
            # and got a 400.
            "percent_bps": DISCOUNT_BPS,
            "max_redemptions": 20,
        },
    )
    assert response.status in (200, 201), response
    issued = response.body["code"]
    assert issued, response.body
    return issued


def test_a_code_can_be_issued(code):
    """Support issues it; the wallet owns the code itself."""
    assert code


@pytest.mark.slow
def test_a_quote_previews_the_code_without_consuming_it(game, funded_buyer, code):
    """A basket page must not spend the code by rendering.

    Asked twice on purpose: if previewing consumed a redemption, the second call would differ.
    """
    first = a.call(
        "GET", f"{a.ORDER}/v1/quotes/{game['id']}?discount_code={code}", user=funded_buyer
    )
    assert first.status == 200, first
    assert first.body["discount"]["amount_minor"] == str(DISCOUNT)
    assert first.body["total_charged"]["amount_minor"] == str(PRICE - DISCOUNT)

    second = a.call(
        "GET", f"{a.ORDER}/v1/quotes/{game['id']}?discount_code={code}", user=funded_buyer
    )
    assert second.body["discount"]["amount_minor"] == str(DISCOUNT)


@pytest.mark.slow
def test_a_purchase_with_a_code_charges_less_and_pays_the_developer_in_full(
    developer, support, admin, code, balance, settled
):
    """The platform absorbs the discount out of its own 30%.

    A developer's income should not drop because the platform ran a promotion they were never
    asked about. Uses its own buyer and game so the totals are unambiguous.
    """
    buyer = a.new_id()
    a.provision_wallet(buyer)
    a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-discount-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed for the discount test",
        },
    )
    buyer_before = balance(buyer)
    developer_before = balance(developer)
    platform_before = balance(a.PLATFORM_USER)

    from test_07_preorders import a_priced_game

    game_id = a_priced_game(developer, support)
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/publish", user=developer, role="DEVELOPER")

    placed = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=buyer, key=f"e2e-discount-{game_id}",
        body={"game_id": game_id, "discount_code": code},
    )
    assert placed.status == 202, placed
    order = settled(placed.body["id"], buyer)
    assert order["state"] == "COMPLETED", order

    # The buyer paid the discounted price.
    assert order["discount"]["amount_minor"] == str(DISCOUNT)
    assert order["discount_code"] == code
    assert order["total_charged"]["amount_minor"] == str(PRICE - DISCOUNT)
    assert buyer_before - balance(buyer) == PRICE - DISCOUNT

    # The developer got the full 70% of the list price; the platform absorbed the reduction.
    assert balance(developer) - developer_before == 700_000
    assert balance(a.PLATFORM_USER) - platform_before == 300_000 - DISCOUNT

    # And the invariant still holds on a discounted order.
    charged = int(order["total_charged"]["amount_minor"])
    assert (
        int(order["developer_share"]["amount_minor"])
        + int(order["platform_share"]["amount_minor"])
        == charged
    )


@pytest.mark.slow
def test_a_nonexistent_code_is_refused_before_anything_is_charged(
    developer, support, funded_buyer, balance
):
    """A fresh game on purpose.

    The first version of this test reused the shared `game`, which the buyer already owns — so
    the ownership check refused it with a 409 and the code was never looked at. The test passed
    for the wrong reason, which is worse than failing.
    """
    from test_07_preorders import a_priced_game

    game_id = a_priced_game(developer, support)
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/publish", user=developer, role="DEVELOPER")

    before = balance(funded_buyer)
    response = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer, key=f"e2e-bad-code-{a.new_id()}",
        body={"game_id": game_id, "discount_code": "NO-SUCH-CODE-XYZ"},
    )
    assert response.status in (404, 422), response
    assert balance(funded_buyer) == before


@pytest.mark.slow
def test_the_code_is_recorded_on_the_order(developer, support, admin, code, settled):
    """A consumed code is a real cost the platform absorbed. Reconciliation and a support agent
    asking "why did they pay less" both need it on the order."""
    buyer = a.new_id()
    a.provision_wallet(buyer)
    a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"e2e-fund-record-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "seed",
        },
    )

    from test_07_preorders import a_priced_game

    game_id = a_priced_game(developer, support)
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/publish", user=developer, role="DEVELOPER")

    placed = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=buyer, key=f"e2e-record-{game_id}",
        body={"game_id": game_id, "discount_code": code},
    )
    order = settled(placed.body["id"], buyer)

    stored = a.psql(
        "order",
        f"SELECT discount_code, discount_minor FROM orders WHERE id = '{order['id']}'",
    )
    assert code in stored, stored
    assert str(DISCOUNT) in stored, stored
