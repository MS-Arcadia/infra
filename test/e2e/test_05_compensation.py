"""The compensation path.

The hardest thing in the platform to reach, and the most important thing to prove: a buyer
whose money moved and whose game did not must end up whole.

It cannot be triggered by simply buying something twice, because the order service's
pre-flight check correctly refuses that before any money moves. It needs a genuine race —
two purchases that both pass the pre-flight and then contend for the same entitlement.
"""

from __future__ import annotations

import concurrent.futures as futures

import arcadia as a
import pytest

from conftest import PRICE


@pytest.mark.slow
def test_racing_two_gifts_to_one_recipient_compensates_the_loser(
    game, funded_buyer, wallets, balance, settled
):
    """Both pass the pre-flight — neither has granted yet — so both debit the buyer.

    One grant then wins and the other is refused with GAME_ALREADY_OWNED, which arrives at
    the saga as an event rather than an exception and triggers the refund. The buyer must end
    up having paid for one game, not two.
    """
    recipient = a.new_id()
    a.provision_wallet(recipient)

    before = balance(funded_buyer)

    def place(suffix: str):
        return a.call(
            "POST", f"{a.ORDER}/v1/gifts", user=funded_buyer,
            key=f"e2e-race-{recipient}-{suffix}",
            body={"game_id": game["id"], "recipient_id": recipient},
        )

    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(place, ["a", "b"]))

    accepted = [r.body for r in results if r.status == 202]

    if len(accepted) < 2:
        # The pre-flight caught the second one. Better than compensating, and not a failure:
        # no money moved at all.
        assert balance(funded_buyer) == before
        pytest.skip("the pre-flight refused the second order; no compensation needed")

    outcomes = sorted(settled(order["id"], funded_buyer)["state"] for order in accepted)
    assert outcomes == ["COMPLETED", "FAILED"], outcomes

    # The whole point: charged once, for one game.
    assert before - balance(funded_buyer) == PRICE

    library = a.call(
        "GET", f"{a.CATALOG}/v1/users/{recipient}/library",
        user=a.new_id(), role="SUPPORT",
    )
    assert library.status == 200, library
    assert library.body["total"] == 1

    # The compensated order says what refused it and that the refund was issued. A silently
    # FAILED order tells a support agent nothing.
    failed = [
        settled(order["id"], funded_buyer)
        for order in accepted
        if settled(order["id"], funded_buyer)["state"] == "FAILED"
    ]
    assert len(failed) == 1
    assert failed[0]["failure_reason"] == "GAME_ALREADY_OWNED"
