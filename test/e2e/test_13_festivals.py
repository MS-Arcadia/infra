"""Festival service, against the running platform.

Requirement 1.9. This service owns three of its four bullets — Admin creates a
festival, selects the games in it, and runs it DRAFT -> ACTIVE -> ENDED /
CANCELLED. The fourth — a discount, proposed by Support and approved by the
developer — is deliberately not reimplemented here: it already exists as
catalog-service's `Promotion` workflow, tagged with this service's
`festival_id`. What is worth testing at this level, across two databases and a
Kafka topic, is that the two halves actually agree:

* a game only becomes selectable here once Catalog's `GamePublished` has
  actually arrived, not merely once the POST that publishes it returns 200;
* a discount the developer approves in Catalog shows up here, with the same
  numbers Catalog itself would show a buyer, and only after approval — not on
  proposal;
* starting a festival reaches auth-profile-service for the platform's active
  user directory and, through that, reaches every one of those users'
  notifications — three services and two hops (one HTTP, one Kafka) for a
  single admin action.

Nothing about the discount arithmetic or the notification's wording is
re-tested here; those belong to catalog-service's and notification-service's
own suites. This file is for the seams between all three.
"""

from __future__ import annotations

import time
import uuid

import pytest

from arcadia import AUTH, CATALOG, FESTIVAL, NOTIFICATION, call, new_id, topic_message_count

# Outbox poll interval plus the consumer's fetch, same budget test_12_marketplace.py
# uses for a single Kafka hop. The promotion-approval tests cross two: Catalog's
# outbox to game-events, and this service's consumer reading it — still one hop of
# actual queueing, so the same budget applies, with a genuinely generous ceiling
# rather than a longer flat sleep.
SETTLE_SECONDS = 8
POLL_TIMEOUT = 20


def fest(method: str, path: str, **kwargs):
    return call(method, FESTIVAL + path, **kwargs)


def cat(method: str, path: str, **kwargs):
    return call(method, CATALOG + path, **kwargs)


def published_game(developer: str, support: str, *, title: str = "Nebula Run") -> dict:
    """A game taken all the way to PUBLISHED.

    Duplicated from conftest's own `game` fixture rather than reusing it: that
    fixture is session-scoped, one game for the whole suite's purchase and refund
    story, and this file needs a fresh, independent game per scenario — in
    particular for the promotion tests, which change a game's live price and
    should not do that to a game other files are mid-transaction with.
    """
    created = cat(
        "POST",
        "/v1/games",
        user=developer,
        role="DEVELOPER",
        body={
            "title": title,
            "description": "for the festival end-to-end suite",
            "min_requirements": "",
        },
    )
    assert created.status == 201, created
    game_id = created.body["id"]

    workflow = [
        ("POST", f"/v1/games/{game_id}/versions",
         {"version": "1.0.0", "file_ref": "placeholder", "size_bytes": 1024},
         "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/submit", None, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/review/start", None, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/review/approve", {"note": "ok"}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/suggest-price",
         {"amount_minor": 400_000}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/price",
         {"amount_minor": 800_000}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/publish", None, "DEVELOPER", developer),
    ]
    for method, path, body, role, user in workflow:
        step = cat(method, path, user=user, role=role, body=body)
        assert step.status in (200, 201), (path, step)
    return step.body


def add_when_known(festival_id: str, admin: str, game_id: str, *, timeout: float = POLL_TIMEOUT):
    """Add a game to a festival, retrying while festival-service has not yet
    caught up with Catalog's `GamePublished`.

    Publishing returns as soon as Catalog commits; this service only learns about
    it once the event has crossed Kafka. A test that added the game immediately
    would be racing the outbox dispatcher's poll interval, not testing the
    platform, so this polls the one endpoint that can distinguish "not known yet"
    (404 GAME_UNKNOWN) from every other failure.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = fest(
            "POST", f"/v1/festivals/{festival_id}/games", user=admin, role="ADMIN",
            body={"game_id": game_id},
        )
        if not (response.status == 404 and response.body.get("reason") == "GAME_UNKNOWN"):
            return response
        last = response
        time.sleep(0.5)
    raise AssertionError(f"festival-service never learned about game {game_id}: {last}")


def discount_on(festival_id: str, game_id: str, *, timeout: float = POLL_TIMEOUT) -> dict | None:
    """Poll a festival's detail view until the given game shows a discount, or
    the deadline passes. Returns the game's entry either way, so a caller
    checking "still no discount" does not have to wait out the timeout."""
    deadline = time.monotonic() + timeout
    entry = None
    while time.monotonic() < deadline:
        response = fest("GET", f"/v1/festivals/{festival_id}")
        assert response.status == 200, response
        entry = next(g for g in response.body["games"] if g["game_id"] == game_id)
        if entry["discount_bps"] is not None:
            return entry
        time.sleep(0.5)
    return entry


PASSWORD = "SuperSecret123!"


def real_active_user() -> str:
    """A user who actually registered through auth-profile-service and was
    approved — not one of this suite's usual self-signed tokens.

    Needed for exactly one thing here: `GET /v1/admin/users/ids?status=ACTIVE`,
    which festival-service calls to build a festival's notification audience,
    reads auth-profile-service's own user table. A token minted locally by this
    suite (as every other test in this file uses) was never registered and
    would never appear in that list, so the audience test would pass by
    accident — an empty audience looking identical to a broken directory call —
    unless at least one real account exists to look for.

    Duplicated from test_00_identity.py's own `register`/`approve` rather than
    imported: importing a fixture or a helper across test modules gives pytest
    two definitions of it, one per importing module, which is the exact bug
    documented at the top of conftest.py.
    """
    email = f"e2e-festival-{uuid.uuid4().hex[:10]}@example.com"
    registered = call(
        "POST", f"{AUTH}/v1/auth/register",
        body={"email": email, "password": PASSWORD, "display_name": "Festival E2E"},
    )
    assert registered.status == 201, registered
    user_id = registered.body["user_id"]

    approved = call(
        "POST", f"{AUTH}/v1/registrations/{user_id}/decide",
        user=new_id(), role="SUPPORT", body={"approve": True},
    )
    assert approved.status in (200, 204), approved
    return user_id


def notified(user_id: str, *, timeout: float = POLL_TIMEOUT) -> list[dict]:
    """Poll a user's notifications until at least one arrives, or the deadline
    passes. Returns whatever is there either way."""
    deadline = time.monotonic() + timeout
    items: list[dict] = []
    while time.monotonic() < deadline:
        response = call("GET", f"{NOTIFICATION}/v1/notifications", user=user_id)
        assert response.status == 200, response
        items = response.body["items"]
        if items:
            return items
        time.sleep(0.5)
    return items


@pytest.fixture
def admin() -> str:
    return new_id()


@pytest.fixture
def support() -> str:
    return new_id()


@pytest.fixture
def developer() -> str:
    return new_id()


@pytest.fixture
def game(developer, support) -> dict:
    return published_game(developer, support)


@pytest.fixture
def festival(admin) -> dict:
    """An empty DRAFT festival, window starting an hour from now."""
    response = fest(
        "POST", "/v1/festivals", user=admin, role="ADMIN",
        body={
            "name": "Summer Sale",
            "description": "the festival end-to-end suite's own festival",
            "starts_at": "2027-06-01T00:00:00Z",
            "ends_at": "2027-06-08T00:00:00Z",
        },
    )
    assert response.status == 201, response
    return response.body


# ------------------------------------------------------------------ authorisation


def test_browsing_festivals_needs_no_token(festival):
    """A sale nobody can see without logging in defeats the point of running one."""
    listing = call("GET", f"{FESTIVAL}/v1/festivals")
    assert listing.status == 200
    assert "items" in listing.body

    detail = call("GET", f"{FESTIVAL}/v1/festivals/{festival['id']}")
    assert detail.status == 200
    assert detail.body["id"] == festival["id"]


@pytest.mark.parametrize("role", ["BASIC_USER", "DEVELOPER", "SUPPORT"])
def test_only_admin_creates_a_festival(role):
    """Requirement 1.9 names the platform itself. Support's part of this story is
    the discount, decided inside Catalog — not standing up the festival."""
    response = fest(
        "POST", "/v1/festivals", user=new_id(), role=role,
        body={"name": "x", "starts_at": "2027-01-01T00:00:00Z", "ends_at": "2027-01-08T00:00:00Z"},
    )
    assert response.status == 403, response
    assert response.body["reason"] == "ROLE_REQUIRED"


def test_creating_a_festival_needs_a_token_at_all():
    response = call(
        "POST", f"{FESTIVAL}/v1/festivals",
        body={"name": "x", "starts_at": "2027-01-01T00:00:00Z", "ends_at": "2027-01-08T00:00:00Z"},
    )
    assert response.status == 401, response


# --------------------------------------------------------------- selecting games


def test_a_game_unknown_to_this_service_cannot_be_selected(festival, admin):
    """Nothing has told this service the game exists — whether because it never
    will, or because Catalog's event has not arrived yet is indistinguishable
    from here, which is exactly why `add_when_known` exists for the positive case."""
    response = fest(
        "POST", f"/v1/festivals/{festival['id']}/games", user=admin, role="ADMIN",
        body={"game_id": new_id()},
    )
    assert response.status == 404, response
    assert response.body["reason"] == "GAME_UNKNOWN"


def test_a_freshly_published_game_becomes_selectable(festival, game, admin):
    """The seam this file exists for: Catalog's `GamePublished` has to actually
    cross Kafka before this service will act on it."""
    response = add_when_known(festival["id"], admin, game["id"])
    assert response.status == 201, response
    assert any(g["game_id"] == game["id"] for g in response.body["games"])

    # And the title came from Catalog, not from the request — the caller never
    # sent one.
    entry = next(g for g in response.body["games"] if g["game_id"] == game["id"])
    assert entry["title"] == game["title"]


def test_the_same_game_cannot_be_selected_twice(festival, game, admin):
    first = add_when_known(festival["id"], admin, game["id"])
    assert first.status == 201, first

    again = fest(
        "POST", f"/v1/festivals/{festival['id']}/games", user=admin, role="ADMIN",
        body={"game_id": game["id"]},
    )
    assert again.status == 409, again
    assert again.body["reason"] == "GAME_ALREADY_IN_FESTIVAL"


def test_removing_a_selected_game(festival, game, admin):
    add_when_known(festival["id"], admin, game["id"])

    removed = fest(
        "DELETE", f"/v1/festivals/{festival['id']}/games/{game['id']}", user=admin, role="ADMIN",
    )
    assert removed.status == 200, removed
    assert not any(g["game_id"] == game["id"] for g in removed.body["games"])


# ------------------------------------------------------------------------ lifecycle


def test_a_festival_with_no_games_refuses_to_start(festival, admin):
    response = fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")
    assert response.status == 422, response
    assert response.body["reason"] == "FESTIVAL_HAS_NO_GAMES"


def test_starting_a_festival_with_a_game_moves_it_to_active(festival, game, admin):
    add_when_known(festival["id"], admin, game["id"])

    started = fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")
    assert started.status == 200, started
    assert started.body["state"] == "ACTIVE"

    # A public GET agrees, without needing a token.
    detail = call("GET", f"{FESTIVAL}/v1/festivals/{festival['id']}")
    assert detail.body["state"] == "ACTIVE"


def test_an_active_festival_cannot_be_started_again(festival, game, admin):
    add_when_known(festival["id"], admin, game["id"])
    fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")

    again = fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")
    assert again.status == 409, again
    assert again.body["reason"] == "FESTIVAL_WRONG_STATE"


def test_ending_and_the_games_stay_on_record(festival, game, admin):
    add_when_known(festival["id"], admin, game["id"])
    fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")

    ended = fest("POST", f"/v1/festivals/{festival['id']}/end", user=admin, role="ADMIN")
    assert ended.status == 200, ended
    assert ended.body["state"] == "ENDED"
    assert any(g["game_id"] == game["id"] for g in ended.body["games"])


def test_a_draft_festival_can_be_cancelled(festival, admin):
    cancelled = fest("POST", f"/v1/festivals/{festival['id']}/cancel", user=admin, role="ADMIN")
    assert cancelled.status == 200, cancelled
    assert cancelled.body["state"] == "CANCELLED"


def test_an_ended_festival_cannot_be_cancelled(festival, game, admin):
    add_when_known(festival["id"], admin, game["id"])
    fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")
    fest("POST", f"/v1/festivals/{festival['id']}/end", user=admin, role="ADMIN")

    response = fest("POST", f"/v1/festivals/{festival['id']}/cancel", user=admin, role="ADMIN")
    assert response.status == 409, response
    assert response.body["reason"] == "FESTIVAL_WRONG_STATE"


# ------------------------------------------------------- the discount, decided in Catalog


def test_a_proposed_but_unapproved_discount_does_not_yet_apply(festival, game, admin, support):
    """Support proposing a discount is not the same as a developer agreeing to
    it, and only the second is a real price change. If this ever showed a
    discount early, a festival page would advertise a price nobody has to honour."""
    add_when_known(festival["id"], admin, game["id"])

    proposed = cat(
        "POST", f"/v1/games/{game['id']}/promotions", user=support, role="SUPPORT",
        body={
            "discount_bps": 2500,
            "starts_at": "2027-06-01T00:00:00Z",
            "ends_at": "2027-06-08T00:00:00Z",
            "festival_id": festival["id"],
            "note": "summer sale",
        },
    )
    assert proposed.status == 201, proposed

    entry = discount_on(festival["id"], game["id"], timeout=SETTLE_SECONDS)
    assert entry["discount_bps"] is None, (
        "a merely-proposed promotion must not show as a discount: " + str(entry)
    )


def test_an_approved_discount_reaches_the_festival_and_agrees_with_catalog(
    festival, game, admin, support, developer
):
    """The whole point of the split: Support proposes and the developer approves
    inside Catalog, tagged with this festival's id, and this service — which
    never decided anything about the price — ends up agreeing with Catalog about
    what it is.
    """
    add_when_known(festival["id"], admin, game["id"])

    proposed = cat(
        "POST", f"/v1/games/{game['id']}/promotions", user=support, role="SUPPORT",
        body={
            "discount_bps": 2500,
            "starts_at": "2027-06-01T00:00:00Z",
            "ends_at": "2027-06-08T00:00:00Z",
            "festival_id": festival["id"],
            "note": "summer sale",
        },
    )
    assert proposed.status == 201, proposed
    promotion_id = proposed.body["promotions"][-1]["id"]

    approved = cat(
        "POST", f"/v1/games/{game['id']}/promotions/{promotion_id}/approve",
        user=developer, role="DEVELOPER", body={"note": "sure"},
    )
    assert approved.status == 200, approved
    assert approved.body["discount_bps"] == 2500

    entry = discount_on(festival["id"], game["id"])
    assert entry is not None and entry["discount_bps"] == 2500, (
        f"festival-service never recorded the approved discount: {entry}"
    )
    # Not just "a number" — the same number a buyer sees on the game page.
    discounted, effective = entry["discounted_price"], approved.body["effective_price"]
    assert discounted["amount_minor"] == effective["amount_minor"]
    assert discounted["currency"] == effective["currency"]


def test_a_rejected_discount_never_shows_as_applied(festival, game, admin, support, developer):
    add_when_known(festival["id"], admin, game["id"])

    proposed = cat(
        "POST", f"/v1/games/{game['id']}/promotions", user=support, role="SUPPORT",
        body={
            "discount_bps": 5000,
            "starts_at": "2027-06-01T00:00:00Z",
            "ends_at": "2027-06-08T00:00:00Z",
            "festival_id": festival["id"],
            "note": "too steep",
        },
    )
    assert proposed.status == 201, proposed
    promotion_id = proposed.body["promotions"][-1]["id"]

    rejected = cat(
        "POST", f"/v1/games/{game['id']}/promotions/{promotion_id}/reject",
        user=developer, role="DEVELOPER", body={"note": "not agreed"},
    )
    assert rejected.status == 200, rejected

    entry = discount_on(festival["id"], game["id"], timeout=SETTLE_SECONDS)
    assert entry["discount_bps"] is None, entry


def test_a_promotion_outside_any_festival_is_none_of_this_services_business(game, support):
    """Support can discount a game with no festival at all — requirement §3.2's
    discount codes are a separate mechanism from this one. An empty festival_id
    must not make this service raise, dead-letter, or invent a festival for it."""
    response = cat(
        "POST", f"/v1/games/{game['id']}/promotions", user=support, role="SUPPORT",
        body={
            "discount_bps": 1000,
            "starts_at": "2027-06-01T00:00:00Z",
            "ends_at": "2027-06-08T00:00:00Z",
            "note": "no festival attached",
        },
    )
    assert response.status == 201, response
    # The proof this did not upset festival-service is in test_99: no dead letter
    # on game-events or festival-events, and the outbox still drains.


def test_starting_a_festival_notifies_the_platforms_active_users(festival, game, admin):
    """The audience for `FestivalStarted` used to be a documented, permanent empty
    list — this service had no user directory and no way to get one. It now
    calls auth-profile-service's internal `/v1/admin/users/ids` synchronously
    from `start`, and that is what this test is actually checking: not the
    wording of the notification, which is notification-service's own concern,
    but that a real, freshly-registered account is reachable through two
    services and a Kafka hop from one admin action.
    """
    user_id = real_active_user()
    add_when_known(festival["id"], admin, game["id"])

    started = fest("POST", f"/v1/festivals/{festival['id']}/start", user=admin, role="ADMIN")
    assert started.status == 200, started

    items = notified(user_id)
    assert any(n.get("subject_type") == "FESTIVAL" for n in items), (
        f"user {user_id} was registered and active before the festival started, "
        f"and was never notified: {items}"
    )


# ------------------------------------------------------------------------- health


def test_no_festival_event_was_dead_lettered():
    """This service's half of the same check test_99 runs for the whole platform,
    kept local too so a failure here names the topic immediately rather than
    sending someone to a parametrized test at the bottom of a different file."""
    depth = topic_message_count("festival-events.dlq")
    assert depth == 0, f"festival-events.dlq holds {depth} message(s) a consumer could not read"
