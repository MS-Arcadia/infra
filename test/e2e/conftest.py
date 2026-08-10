"""Fixtures for the end-to-end suite.

Two decisions shape this file.

**Every run uses fresh user ids.** The alternative is a suite that only passes against a
wiped volume, which makes it something people stop running. Fresh UUIDs per session mean
`pytest` twice in a row passes twice in a row.

**The stack is checked before anything else runs.** A suite that fails with a connection
error tells you nothing about the platform; one that says "media-service is not healthy"
tells you exactly what to do.
"""

from __future__ import annotations

import time

import arcadia as a
import pytest

# Every service, and the endpoint that proves it is not merely listening.
SERVICES = {
    "auth-profile-service": a.AUTH,
    "wallet-service": a.WALLET,
    "payment-service": a.PAYMENT,
    "catalog-service": a.CATALOG,
    "order-service": a.ORDER,
    "media-service": a.MEDIA,
    "notification-service": a.NOTIFICATION,
    "recommendation-service": a.RECOMMENDATION,
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: waits on the saga or a background job")


@pytest.fixture(scope="session", autouse=True)
def stack_is_up() -> None:
    """Refuse to run against a stack that is not ready, and say which part is not.

    Checked once. Every failure after this is about the platform's behaviour rather than
    about whether it is running, which is the difference between a useful failure and a
    confusing one.
    """
    unhealthy = []
    for name, base in SERVICES.items():
        response = a.call("GET", f"{base}/readyz")
        if response.status != 200:
            detail = response.body if response.body else response.raw[:200]
            unhealthy.append(f"{name}: {response.status} {detail}")
    if unhealthy:
        pytest.exit(
            "the platform is not ready:\n  "
            + "\n  ".join(unhealthy)
            + "\n\nStart it with:  cd infra && make up && make wait",
            returncode=1,
        )


# --- the people in the story ---------------------------------------------
#
# Session-scoped so the whole suite tells one story, and freshly generated so the story can
# be told again without resetting anything.


@pytest.fixture(scope="session")
def developer() -> str:
    return a.new_id()


@pytest.fixture(scope="session")
def buyer() -> str:
    return a.new_id()


@pytest.fixture(scope="session")
def friend() -> str:
    """The gift recipient."""
    return a.new_id()


@pytest.fixture(scope="session")
def stranger() -> str:
    """A third party, for the compensation race."""
    return a.new_id()


@pytest.fixture(scope="session")
def support() -> str:
    return a.new_id()


@pytest.fixture(scope="session")
def admin() -> str:
    return a.new_id()


@pytest.fixture(scope="session")
def wallets(developer, buyer, friend, stranger) -> dict[str, str]:
    """A wallet for everyone who will hold money.

    See `arcadia.provision_wallet` for why this is an HTTP call rather than a UserRegistered
    event on Kafka.
    """
    people = {"developer": developer, "buyer": buyer, "friend": friend, "stranger": stranger}
    for user_id in [*people.values(), a.PLATFORM_USER]:
        a.provision_wallet(user_id)
    return people


@pytest.fixture(scope="session")
def funded_buyer(wallets, buyer, admin) -> str:
    """A buyer with money.

    Through the admin adjustment endpoint rather than by writing to the database, so the
    ledger entry exists and the reconciliation assertions at the end of the suite are
    meaningful.
    """
    response = a.call(
        "POST",
        f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust",
        user=admin,
        role="ADMIN",
        key=f"e2e-fund-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "10000000", "currency": "IRR"},
            "reason": "seed for the end-to-end suite",
        },
    )
    assert response.status == 200, response
    return buyer


# --- helpers -------------------------------------------------------------


@pytest.fixture(scope="session")
def balance():
    """The current balance of a wallet, in minor units."""

    def read(user_id: str) -> int:
        response = a.call("GET", f"{a.WALLET}/v1/wallets/me", user=user_id)
        assert response.status == 200, response
        return int(response.body["balance"]["amount_minor"])

    return read


@pytest.fixture(scope="session")
def settled():
    """Wait for an order to leave PENDING, and return it.

    Polls rather than sleeping a fixed time: the saga normally settles in a few seconds, and a
    fixed wait would either be slow or flaky depending on the machine.
    """

    def wait(order_id: str, user: str, *, timeout: float = 45.0) -> dict:
        deadline = time.monotonic() + timeout
        order = None
        while time.monotonic() < deadline:
            response = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=user)
            assert response.status == 200, response
            order = response.body
            if order["state"] != "PENDING":
                return order
            time.sleep(0.5)
        raise AssertionError(
            f"order {order_id} was still PENDING after {timeout}s.\n"
            f"Check `docker logs arcadia-order` and the saga step in "
            f"GET /v1/users/{{id}}/orders.\nLast seen: {order}"
        )

    return wait

# --- the story the suite tells -------------------------------------------
#
# These live here rather than in a test module for one specific reason: pytest resolves
# fixtures by definition, and `from test_01_publishing import game` creates a SECOND
# definition in the importing module. A session-scoped fixture then runs once per importing
# module. That is not a subtlety — it made this suite buy the game twice and then assert that
# one purchase had been refunded, and the failure looked like a broken reversal in the wallet.

PRICE = 1_000_000
SUGGESTED_PRICE = 500_000
DEVELOPER_SHARE = 700_000
PLATFORM_SHARE = 300_000
GIFT_MESSAGE_FEE = 20_000


@pytest.fixture(scope="session")
def game(developer, support) -> dict:
    """A game taken all the way to PUBLISHED. Everything downstream needs something to sell."""
    created = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        user=developer,
        role="DEVELOPER",
        body={
            "title": "Neon Drift",
            "description": "A neon racing game, for the end-to-end suite.",
            "min_requirements": "4 GB RAM",
            "genres": ["Racing", "Indie"],
        },
    )
    assert created.status == 201, created
    game_id = created.body["id"]

    workflow = [
        ("POST", f"/v1/games/{game_id}/versions",
         {"version": "1.0.0", "file_ref": "placeholder", "size_bytes": 2048},
         "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/submit", None, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/review/start", None, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/review/approve", {"note": "looks good"}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/suggest-price", {"amount_minor": SUGGESTED_PRICE},
         "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/price", {"amount_minor": PRICE}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/publish", None, "DEVELOPER", developer),
    ]
    for method, path, body, role, user in workflow:
        step = a.call(method, f"{a.CATALOG}{path}", user=user, role=role, body=body)
        # 201 for the version, 200 for the workflow steps.
        assert step.status in (200, 201), (path, step)

    return step.body  # the published game


@pytest.fixture(scope="session")
def purchase(game, funded_buyer, wallets, settled, balance) -> dict:
    """One completed purchase, and the balances as they were before it.

    Recorded rather than assumed: the suite makes several purchases, so an assertion against
    an absolute balance would be coupled to how many tests ran first.
    """
    before = {"buyer": balance(funded_buyer), "platform": balance(a.PLATFORM_USER)}

    accepted = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=funded_buyer,
        key=f"e2e-buy-{game['id']}", body={"game_id": game["id"]},
    )
    # 202, not 201: the order exists but nothing has been charged or granted yet.
    assert accepted.status == 202, accepted
    assert accepted.body["state"] == "PENDING"

    order = settled(accepted.body["id"], funded_buyer)
    assert order["state"] == "COMPLETED", order
    return {"accepted": accepted.body, "order": order, "before": before}


@pytest.fixture(scope="session")
def completed_order(purchase) -> dict:
    return purchase["order"]


@pytest.fixture(scope="session")
def gift_order(game, funded_buyer, friend, wallets, settled) -> dict:
    """A completed gift, with a message, so the 2% surcharge is exercised."""
    accepted = a.call(
        "POST", f"{a.ORDER}/v1/gifts", user=funded_buyer,
        key=f"e2e-gift-{game['id']}",
        body={
            "game_id": game["id"],
            "recipient_id": friend,
            "message": "happy birthday, enjoy the game",
        },
    )
    assert accepted.status == 202, accepted
    order = settled(accepted.body["id"], funded_buyer)
    assert order["state"] == "COMPLETED", order
    return order
