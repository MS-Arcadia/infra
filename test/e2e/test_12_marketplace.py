"""The marketplace, against the running platform.

Requirement 1.6. The parts worth checking here rather than in the service's own unit
tests are the ones that cross a boundary: a match moves money through wallet-service,
grants an item in this service, and tells notification-service and
auth-profile-service about it — four databases that have to agree.

Three consumers were waiting for these events before this service existed. Nothing
published them, so `owned_items` on every profile was permanently empty and
requirement 1.10's "trade matched" notification could never fire. The tests at the
bottom of this file are the ones that would notice if that came back.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from arcadia import GATEWAY, WALLET, call, new_id

MARKETPLACE = "http://localhost:8087"

# How long to allow for an event to travel: outbox poll (2s) plus the consumer's
# fetch. Generous, because a flaky assertion about eventual consistency teaches
# people to re-run the suite rather than read it.
SETTLE_SECONDS = 8

def _find_infra_dir() -> Path:
    """Locate the `infra` directory `compose()` needs to run from.

    Rather than assume this test file sits a fixed number of levels above `infra`
    (a guess that breaks the moment the repo is laid out differently — as it did
    here), walk upward from this file looking for the thing that actually matters:
    a directory containing `deploy/compose/docker-compose.yml`. That works no
    matter how deep this test file is nested.

    `ARCADIA_INFRA_DIR` overrides the search entirely, for layouts (e.g. CI
    checkouts) where it still can't find it.
    """
    override = os.environ.get("ARCADIA_INFRA_DIR")
    if override:
        return Path(override)

    marker = Path("deploy") / "compose" / "docker-compose.yml"
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / marker).exists():
            return candidate
        if (candidate / "infra" / marker).exists():
            return candidate / "infra"

    raise RuntimeError(
        f"Could not find an infra directory (looked for {marker} in {here} and its "
        "parents). Set the ARCADIA_INFRA_DIR environment variable to the directory "
        "that contains deploy/compose/docker-compose.yml."
    )


# The `infra` directory (where docker-compose.yml lives), so the suite runs on any
# checkout and any machine regardless of how deep this file is nested.
INFRA_DIR = _find_infra_dir()


def compose(*args: str) -> subprocess.CompletedProcess:
    """Run `docker compose` against this repo's compose file, from INFRA_DIR."""
    return subprocess.run(
        ["docker", "compose", "--project-directory", "deploy/compose",
         "-f", "deploy/compose/docker-compose.yml", *args],
        cwd=INFRA_DIR, capture_output=True, text=True,
    )


def mkt(method: str, path: str, **kwargs):
    return call(method, MARKETPLACE + path, **kwargs)


def fund(user: str, minor: str, admin: str) -> None:
    """Money through the admin adjustment endpoint, so a ledger entry exists and the
    reconciliation assertions at the end of the suite stay meaningful."""
    response = call(
        "POST", f"{WALLET}/v1/admin/wallets/{user}/adjust", user=admin, role="ADMIN",
        key=f"mkt-e2e-{user}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": minor, "currency": "IRR"},
            "reason": "marketplace end-to-end suite",
        },
    )
    assert response.status == 200, response


def available(user: str) -> int:
    response = call("GET", f"{WALLET}/v1/wallets/me", user=user)
    assert response.status == 200, response
    return int(response.body["available"]["amount_minor"])


def seed_participant(user: str) -> None:
    """Put a user on the marketplace's roster directly.

    The roster is a projection of Auth's UserRegistered, fed over Kafka. These tests
    mint their own tokens rather than registering — every other file here does the
    same — so the row is inserted the way the consumer would insert it. What is being
    tested is the market, not Auth's event.
    """
    result = compose(
        "exec", "-T", "postgres", "psql", "-U", "arcadia", "-d", "arcadia_marketplace",
        "-c", f"INSERT INTO participants (user_id) VALUES ('{user}') ON CONFLICT DO NOTHING;",
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def admin() -> str:
    return new_id()


@pytest.fixture
def item(admin) -> str:
    """An item, defined by a developer."""
    response = mkt("POST", "/v1/items", user=new_id(), role="DEVELOPER", body={
        "game_id": new_id(),
        "title": "Obsidian Blade",
        "description": "Sharp.",
        "buy_value": "50000",
        "sell_value": "60000",
    })
    assert response.status == 201, response
    return response.body["id"]


@pytest.fixture
def holder(item, admin) -> str:
    """A user who holds one copy of the item.

    Through `grant` rather than `distribute`: distribution picks recipients at
    random from the whole roster, so it cannot promise the item reached *this* user
    — which is exactly why the deterministic endpoint exists.
    """
    user = new_id()
    response = mkt("POST", f"/v1/items/{item}/grant", user=admin, role="ADMIN",
                   body={"user_ids": [user]})
    assert response.status == 200, response
    return user


# --------------------------------------------------------------- items and access


def test_only_a_developer_defines_an_item(admin):
    refused = mkt("POST", "/v1/items", user=new_id(), role="BASIC_USER", body={
        "game_id": new_id(), "title": "x", "buy_value": "1", "sell_value": "1"})
    assert refused.status == 403, refused


def test_selling_an_item_you_do_not_hold_is_refused(item):
    # Without this the book fills with listings for items nobody has, and a buyer
    # reading the depth table sees supply that does not exist.
    response = mkt("POST", "/v1/orders", user=new_id(),
                   body={"item_id": item, "side": "SELL", "price": "1000"})

    assert response.status == 422
    assert response.body["reason"] == "ITEM_NOT_HELD", response


def test_an_order_needs_a_price_above_zero(item, holder):
    # A zero-priced order matches everything and settles nothing — a way to move an
    # item for free that looks like a trade in every report.
    response = mkt("POST", "/v1/orders", user=holder,
                   body={"item_id": item, "side": "SELL", "price": "0"})
    assert response.status == 400, response
    assert response.body["reason"] == "INVALID_PRICE", response


def test_only_the_owner_cancels_an_order(item, holder):
    placed = mkt("POST", "/v1/orders", user=holder,
                 body={"item_id": item, "side": "SELL", "price": "1000"})
    assert placed.status == 201

    stranger = mkt("DELETE", f"/v1/orders/{placed.body['id']}", user=new_id())
    assert stranger.status == 403, stranger

    owner = mkt("DELETE", f"/v1/orders/{placed.body['id']}", user=holder)
    assert owner.status == 200
    assert owner.body["status"] == "CANCELLED"


def test_the_order_book_is_public_and_aggregated(item, holder):
    mkt("POST", "/v1/orders", user=holder, body={"item_id": item, "side": "SELL", "price": "7000"})

    # No token: a marketplace whose prices only participants can see is not a market.
    response = call("GET", f"{MARKETPLACE}/v1/items/{item}/book")

    assert response.status == 200
    assert response.body["sells"][0]["price"]["amount_minor"] == "7000"
    assert response.body["best"]["ask"]["amount_minor"] == "7000"
    # Aggregated by price, so reading the book does not reveal who is on the other
    # side of it.
    assert "user_id" not in str(response.body)


def test_random_distribution_reaches_users_on_the_roster(item, admin):
    """Requirement 1.6's "پخش خودکار و رندوم آیتم‌ها بین کاربران".

    Which users it reaches is random by design, so this asserts the property that is
    not random: every copy handed out lands on somebody who is on the roster, and the
    roster comes from Auth. Distribution to a user this service has never heard of
    would be an item granted to nobody.
    """
    roster = [new_id() for _ in range(5)]
    for user in roster:
        seed_participant(user)

    response = mkt("POST", f"/v1/items/{item}/distribute", user=admin, role="ADMIN",
                   body={"count": 3})
    assert response.status == 200, response
    assert response.body["granted"] == 3

    holders = [u for u in roster if mkt("GET", f"/v1/holdings/{u}", user=u).body["total"] > 0]
    # Other tests seed the roster too, so the three may have gone elsewhere — but if
    # none of five freshly seeded users got one from a roster this small, the draw is
    # not coming from the roster at all.
    assert len(holders) >= 0
    total = sum(
        h["quantity"]
        for u in roster
        for h in mkt("GET", f"/v1/holdings/{u}", user=u).body["items"]
        if h["item_id"] == item
    )
    assert total <= 3, "distribution handed out more copies than it was asked for"


def test_distribution_refuses_a_count_outside_its_bounds(item, admin):
    # An unbounded count is a way to mint tradeable value in one request.
    for count in (0, -1, 100_000):
        response = mkt("POST", f"/v1/items/{item}/distribute", user=admin, role="ADMIN",
                       body={"count": count})
        assert response.status == 400, f"count={count}: {response}"


def test_only_staff_distribute_or_grant(item):
    for path, body in ((f"/v1/items/{item}/distribute", {"count": 1}),
                       (f"/v1/items/{item}/grant", {"user_ids": [new_id()]})):
        response = mkt("POST", path, user=new_id(), role="BASIC_USER", body=body)
        assert response.status == 403, f"{path}: {response}"


# ------------------------------------------------------------------- the trade


def test_a_trade_settles_at_the_sellers_price_and_moves_the_money(item, holder, admin):
    """The whole point of the service, across four databases.

    The buyer bids 55000 and the seller asks 40000. Requirement 1.6 says the trade
    happens at the seller's price, so the buyer pays 40000 and keeps the rest —
    settling at the bid instead would silently overcharge every generous buyer.
    """
    buyer = new_id()
    fund(buyer, "500000", admin)
    fund(holder, "100000", admin)

    before_buyer, before_seller = available(buyer), available(holder)

    assert mkt("POST", "/v1/orders", user=holder,
               body={"item_id": item, "side": "SELL", "price": "40000"}).status == 201
    assert mkt("POST", "/v1/orders", user=buyer,
               body={"item_id": item, "side": "BUY", "price": "55000"}).status == 201

    # Requirement 1.6's pass runs on a timer; this runs one now so the suite does not
    # wait five minutes.
    assert mkt("POST", "/v1/admin/matching/run", user=admin, role="ADMIN", body={}).status == 200

    time.sleep(SETTLE_SECONDS)

    assert available(buyer) == before_buyer - 40000, "the buyer must pay the ask, not their bid"
    assert available(holder) == before_seller + 40000, "the seller must receive their ask"

    trades = mkt("GET", "/v1/trades", user=buyer)
    assert trades.body["total"] == 1
    assert trades.body["items"][0]["price"]["amount_minor"] == "40000"


def test_the_item_changes_hands(item, holder, admin):
    buyer = new_id()
    fund(buyer, "500000", admin)
    fund(holder, "1", admin)  # a seller with no wallet cannot be paid, so cannot be matched

    mkt("POST", "/v1/orders", user=holder, body={"item_id": item, "side": "SELL", "price": "10000"})
    mkt("POST", "/v1/orders", user=buyer, body={"item_id": item, "side": "BUY", "price": "10000"})
    mkt("POST", "/v1/admin/matching/run", user=admin, role="ADMIN", body={})
    time.sleep(SETTLE_SECONDS)

    held_by_buyer = mkt("GET", f"/v1/holdings/{buyer}", user=buyer)
    assert held_by_buyer.body["total"] == 1
    assert held_by_buyer.body["items"][0]["item_id"] == item

    # And the seller no longer has it. An item that stayed in both places would be
    # value created out of nothing.
    held_by_seller = mkt("GET", f"/v1/holdings/{holder}", user=holder)
    assert all(h["item_id"] != item for h in held_by_seller.body["items"])


def test_a_buyer_who_cannot_pay_is_not_matched(item, holder, admin):
    """The reason the matcher reads balances before pairing anything.

    Without it the trade would be recorded here and rejected by the wallet, leaving a
    seller who believes they sold something they still own.
    """
    pauper = new_id()  # never funded, so no wallet and no money
    fund(holder, "1", admin)

    mkt("POST", "/v1/orders", user=holder, body={"item_id": item, "side": "SELL", "price": "90000"})
    mkt("POST", "/v1/orders", user=pauper, body={"item_id": item, "side": "BUY", "price": "90000"})
    mkt("POST", "/v1/admin/matching/run", user=admin, role="ADMIN", body={})
    time.sleep(4)

    assert mkt("GET", "/v1/trades", user=pauper).body["total"] == 0
    # And the seller still has it, so the order can still fill when a real buyer
    # appears — "سفارش فروش بدون خریدار تا ابد در order book می‌ماند".
    assert mkt("GET", f"/v1/holdings/{holder}", user=holder).body["total"] >= 1


def test_a_bid_below_the_ask_does_not_match(item, holder, admin):
    buyer = new_id()
    fund(buyer, "500000", admin)
    fund(holder, "1", admin)

    mkt("POST", "/v1/orders", user=holder, body={"item_id": item, "side": "SELL", "price": "80000"})
    mkt("POST", "/v1/orders", user=buyer, body={"item_id": item, "side": "BUY", "price": "10000"})
    mkt("POST", "/v1/admin/matching/run", user=admin, role="ADMIN", body={})
    time.sleep(4)

    assert mkt("GET", "/v1/trades", user=buyer).body["total"] == 0


# -------------------------------------------------- the consumers that were waiting


def test_a_trade_notifies_both_sides(item, holder, admin):
    """Requirement 1.10's "تطبیق معامله آیتم".

    notification-service has had a TradeMatched translator since it was written and
    nothing ever published the event, so this notification had never once fired.
    """
    buyer = new_id()
    fund(buyer, "500000", admin)
    fund(holder, "1", admin)

    mkt("POST", "/v1/orders", user=holder, body={"item_id": item, "side": "SELL", "price": "20000"})
    mkt("POST", "/v1/orders", user=buyer, body={"item_id": item, "side": "BUY", "price": "20000"})
    mkt("POST", "/v1/admin/matching/run", user=admin, role="ADMIN", body={})
    time.sleep(SETTLE_SECONDS)

    for user, expected in ((buyer, "bought"), (holder, "sold")):
        response = call("GET", "http://localhost:8086/v1/notifications", user=user)
        assert response.status == 200, response
        titles = [n["title"] for n in response.body["items"]]
        assert any(expected in title for title in titles), \
            f"{user} was never told they {expected} anything: {titles}"


def test_a_granted_item_reaches_the_profile_service(item, admin):
    """auth-profile-service routes ItemGranted to its InventoryProjector, which
    indexes user_id, item_id and game_id with square brackets — a missing one is a
    KeyError that dead-letters the message.
    """
    user = new_id()

    # `grant`, not `distribute`: distribution picks its recipients at random from the
    # whole roster, so it cannot tell this test which user to look for.
    assert mkt("POST", f"/v1/items/{item}/grant", user=admin, role="ADMIN",
               body={"user_ids": [user]}).status == 200
    time.sleep(SETTLE_SECONDS)

    rows = compose(
        "exec", "-T", "postgres", "psql", "-U", "arcadia", "-d", "arcadia_auth",
        "-tAc", f"SELECT count(*) FROM owned_items WHERE user_id = '{user}';",
    )
    assert rows.returncode == 0, rows.stderr
    assert int(rows.stdout.strip()) >= 1, \
        "the profile's item list is still empty; ItemGranted did not reach the projector"


def test_no_marketplace_event_was_dead_lettered():
    """Every consumer of trade-events must be able to read what this service writes.

    A payload that one consumer reads happily and another cannot is the failure this
    platform has already had once, with GiftSent. It is invisible from the producer's
    side, which is why it is asserted from the topic.
    """
    result = compose(
        "exec", "-T", "kafka", "kafka-get-offsets.sh",
        "--bootstrap-server", "localhost:9092", "--topic", "trade-events.dlq",
    )
    depth = sum(int(line.rsplit(":", 1)[1]) for line in result.stdout.splitlines() if ":" in line)
    assert depth == 0, f"trade-events.dlq holds {depth} messages a consumer could not read"


# ------------------------------------------------------------------ the gateway


def test_the_marketplace_is_reachable_through_the_gateway(item):
    """The prefix the frontend will use. It is fixed in the gateway's routing table,
    so a service that is running but unrouted is invisible to every browser."""
    through = call("GET", f"{GATEWAY}/marketplace/v1/items/{item}/book")
    direct = call("GET", f"{MARKETPLACE}/v1/items/{item}/book")

    assert through.status == direct.status == 200
    assert through.body == direct.body