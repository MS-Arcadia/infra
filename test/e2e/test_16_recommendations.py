"""Personalised recommendations, Requirements §3.1 — the competitive differentiator.

Numbered 16 so it runs after games exist, purchases have settled and reviews have been posted — every
signal this service consumes is produced by a file before it — and before `test_99` judges the platform.

This service is the strongest case in the suite for an end-to-end test, because **it has no API that
changes anything.** Everything it knows arrives on three topics owned by three other services:

* `game-events` from Catalog gives it something to recommend, and the genres and tags it ranks by.
* `purchase-events` from Order tells it who owns what — both halves of the hybrid depend on this.
* `review-events` from Review is the only signal that can be negative.

Its own test suite can assert that a taste vector moves when a handler is called. Nothing but a running
platform can assert that Catalog's `_game_payload` still spells the field `genres`, that Order still carries
`recipient_id` rather than only `buyer_id`, or that Review still sends `sentiment: "LIKE"`. Those three
strings are a contract with three repositories, and there is no compiler that checks them.

The ranking assertions here are deliberately about *order*, never about absolute scores. The weights behind
them are a tuning decision that should be free to change; that a racing fan is offered another racing game
before a strategy game is the behaviour, and it is what this file defends.
"""

from __future__ import annotations

import time
import uuid

import arcadia as a
import pytest

# The service's own port. There is no gateway prefix for it yet — `/recommendations` is not in the
# gateway's routing table — so unlike test_14 there is no direct-versus-gateway comparison to make here.
RECOMMENDATION = "http://localhost:8093"
REVIEW = "http://localhost:8088"

# How long to wait for a purchase to cross Kafka into the read-model. Generous because it is three hops:
# the order saga settles, Order's outbox dispatches, then this service's consumer commits.
INGEST_TIMEOUT = 45.0

# Genres are suffixed with a fresh token every run, so this file's three games occupy a corner of the
# content space nothing else has ever been in.
#
# Without it the suite stops being repeatable in a way that is easy to miss: every previous run left its own
# "Turbo Rush E2E" behind, all of them genuinely similar to each other, and `/similar` returns a bounded
# list. After a dozen runs the game this file just published is ranked out of its own assertion by its
# predecessors — a failure that says "not similar" about the one thing that is most similar of all.
RUN = uuid.uuid4().hex[:8]
RACING = f"racing-{RUN}"
INDIE = f"indie-{RUN}"
ACTION = f"action-{RUN}"
STRATEGY = f"strategy-{RUN}"


# --- helpers -------------------------------------------------------------


def reco(method: str, path: str, **kwargs) -> a.Response:
    return a.call(method, RECOMMENDATION + path, **kwargs)


def publish_game(developer: str, support: str, title: str, genres: list[str]) -> str:
    """A game taken to PUBLISHED, returning its id.

    The same seven steps `conftest.game` runs, repeated because this file needs three games with *chosen*
    genres — the genres are the whole experiment, and the session fixture's are fixed.
    """
    created = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        user=developer,
        role="DEVELOPER",
        body={
            "title": title,
            "description": "for the recommendation end-to-end tests",
            "genres": genres,
        },
    )
    assert created.status == 201, created
    game_id = created.body["id"]

    workflow = [
        ("POST", f"/v1/games/{game_id}/versions",
         {"version": "1.0.0", "file_ref": "placeholder", "size_bytes": 2048}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/submit", None, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/review/start", None, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/review/approve", {"note": "looks good"}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/suggest-price", {"amount_minor": 500_000}, "SUPPORT", support),
        ("POST", f"/v1/games/{game_id}/price", {"amount_minor": 1_000_000}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/publish", None, "DEVELOPER", developer),
    ]
    for method, path, body, role, user in workflow:
        step = a.call(method, f"{a.CATALOG}{path}", user=user, role=role, body=body)
        assert step.status in (200, 201), (path, step)
    return game_id


def buy(buyer: str, game_id: str, settled) -> dict:
    accepted = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=buyer,
        key=f"e2e-reco-{buyer}-{game_id}", body={"game_id": game_id},
    )
    assert accepted.status == 202, accepted
    order = settled(accepted.body["id"], buyer)
    assert order["state"] == "COMPLETED", order
    return order


def fund(user_id: str, admin: str, amount: int = 10_000_000) -> str:
    a.provision_wallet(user_id)
    response = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{user_id}/adjust", user=admin, role="ADMIN",
        key=f"e2e-reco-fund-{user_id}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": str(amount), "currency": "IRR"},
            "reason": "seed for the recommendation end-to-end tests",
        },
    )
    assert response.status == 200, response
    return user_id


def refresh(support: str) -> dict:
    """Force the batch sweep instead of waiting for its five-minute schedule.

    The endpoint exists for exactly this and for an operator investigating a complaint. Waiting for the
    scheduler would make this file the slowest in the suite by two orders of magnitude.
    """
    response = reco("POST", "/v1/admin/recommendations/refresh", user=support, role="SUPPORT")
    assert response.status == 200, response
    return response.body


def recommendations_for(user_id: str, support: str, *, limit: int = 20) -> dict:
    response = reco(
        "GET", f"/v1/users/{user_id}/recommendations?limit={limit}", user=support, role="SUPPORT"
    )
    assert response.status == 200, response
    return response.body



def ids(payload: dict) -> list[str]:
    return [item["game_id"] for item in payload["items"]]


def eventually(predicate, *, timeout: float = INGEST_TIMEOUT, message: str = ""):
    """Poll until `predicate` returns something truthy, and return it.

    Every assertion in this file depends on an event having crossed Kafka, so the alternative to polling is
    a fixed sleep long enough for the slowest machine — which is either flaky or slow, and usually both.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(1.0)
    raise AssertionError(f"{message or 'condition never held'} after {timeout}s. Last: {last}")


# --- the cast ------------------------------------------------------------


@pytest.fixture(scope="session")
def reco_developer() -> str:
    return a.new_id()


@pytest.fixture(scope="session")
def racing_fan(admin) -> str:
    """Buys a racing game. The user every ranking assertion below is about."""
    return fund(a.new_id(), admin)


@pytest.fixture(scope="session")
def neighbour(admin) -> str:
    """Buys the same racing game *and* a strategy game.

    This user exists only to create a co-purchase edge: they are the "people who bought this also bought
    that" in Requirements §3.1, and without a second buyer the collaborative half has nothing to say.
    """
    return fund(a.new_id(), admin)


@pytest.fixture(scope="session")
def catalogue(reco_developer, support) -> dict[str, str]:
    """Three games, chosen so content similarity and popularity disagree.

    `racing_owned` and `racing_similar` share a genre; `strategy` shares nothing with either. The
    recommendation for a racing fan is therefore predictable from the genres alone, which is what makes the
    ordering assertion meaningful rather than a restatement of whatever the ranker happened to output.
    """
    return {
        "racing_owned": publish_game(reco_developer, support, f"Neon Drift {RUN}", [RACING, INDIE]),
        "racing_similar": publish_game(reco_developer, support, f"Turbo Rush {RUN}", [RACING, ACTION]),
        "strategy": publish_game(reco_developer, support, f"Empire Dawn {RUN}", [STRATEGY]),
    }


@pytest.fixture(scope="session")
def purchases(catalogue, racing_fan, neighbour, settled) -> dict[str, str]:
    """The signals. Both users buy the racing game; only the neighbour also buys the strategy game."""
    buy(racing_fan, catalogue["racing_owned"], settled)
    buy(neighbour, catalogue["racing_owned"], settled)
    buy(neighbour, catalogue["strategy"], settled)
    return catalogue


@pytest.fixture(scope="session")
def generated(purchases, racing_fan, support) -> dict:
    """Wait for the purchases to be ingested, then force a sweep and return the racing fan's list.

    The wait is on the *result* rather than on a log line or a sleep: a refresh that produced nothing means
    the events have not landed yet, so the loop refreshes again.
    """
    return eventually(
        lambda: _refreshed_list(racing_fan, support),
        message="the racing fan never received a personalised list",
    )


def _refreshed_list(user_id: str, support: str) -> dict | None:
    refresh(support)
    payload = recommendations_for(user_id, support)
    return payload if payload["source"] != "FALLBACK" and payload["items"] else None


@pytest.fixture(scope="session")
def with_co_purchase(generated, racing_fan, catalogue, support) -> dict:
    """The racing fan's list, once the neighbour's purchases have also been ingested.

    Separate from `generated` because the two halves of the hybrid become visible at different times: the
    content match appears as soon as the fan's own purchase lands, while the collaborative edge needs the
    *neighbour's* two purchases through the saga as well.

    This waits rather than skipping. An earlier version skipped when the edge was late, which meant the
    collaborative half could stop working entirely and this file would report nothing but green skips.
    """
    return eventually(
        lambda: _list_containing(racing_fan, support, catalogue["strategy"]),
        message=(
            "the co-purchase edge never appeared: the strategy game shares no genre with anything the "
            "racing fan owns, so item-item filtering is the only way it can be recommended at all"
        ),
    )


def _list_containing(user_id: str, support: str, game_id: str) -> dict | None:
    payload = _refreshed_list(user_id, support)
    return payload if payload and game_id in ids(payload) else None


# --- the service is up ---------------------------------------------------


def test_the_recommendation_service_is_ready():
    response = a.call("GET", f"{RECOMMENDATION}/readyz")
    assert response.status == 200, response
    body = response.body
    assert body["status"] == "UP", body
    # Persistence and the outbox dispatcher are the two that may not be skipped: without the first there is
    # nothing to read, without the second every RecommendationGenerated piles up unpublished.
    assert body["checks"]["persistence"]["status"] == "UP", body
    assert body["checks"]["outbox_dispatcher"]["status"] == "UP", body


def test_the_scheduler_is_running():
    """The batch sweep is what keeps lists fresh without anyone asking. Reported by readiness but never
    fatal — a replica whose sweep stopped can still serve and still ingest, and pulling it out of the load
    balancer would turn a stale recommendation into no recommendation."""
    body = a.call("GET", f"{RECOMMENDATION}/readyz").body
    assert body["checks"].get("generation_scheduler", {}).get("status") == "UP", body


# --- ingest: three contracts this service does not own -------------------


def test_a_published_game_becomes_recommendable(catalogue):
    """Catalog's `GamePublished` reached the read-model, genres and all.

    `/similar` answering 200 is the proof: it 404s for a game this service has never heard of, and it can
    only produce a non-empty answer if the genres arrived — which is the field name this test really checks.
    """
    game_id = catalogue["racing_owned"]
    response = eventually(
        lambda: _similar_or_none(game_id),
        message=f"game {game_id} never reached the recommendation read-model",
    )
    assert response["game_id"] == game_id


def _similar_or_none(game_id: str) -> dict | None:
    response = a.call("GET", f"{RECOMMENDATION}/v1/games/{game_id}/similar")
    return response.body if response.status == 200 else None


def test_similar_games_are_found_by_shared_genre(catalogue):
    """The content half, with no user involved: two racing games are neighbours, a strategy game is not."""
    body = eventually(
        lambda: _similar_or_none(catalogue["racing_owned"]),
        message="the racing game never became known",
    )
    neighbours = eventually(
        lambda: body if (body := _similar_or_none(catalogue["racing_owned"])) and body["items"] else None,
        message="the racing game never found a neighbour",
    )
    found = {item["game_id"]: item for item in neighbours["items"]}
    assert catalogue["racing_similar"] in found, neighbours
    assert f"genre:{RACING}" in found[catalogue["racing_similar"]]["shared_features"], found

    # The strategy game shares no genre and no tag, so it must not appear at all — a cosine of zero is
    # dropped rather than ranked last.
    assert catalogue["strategy"] not in found, neighbours

    # A game is never similar to itself: it would otherwise top every rail on its own page.
    assert catalogue["racing_owned"] not in found, neighbours


def test_a_purchase_produces_a_personalised_list(generated):
    """`PurchaseCompleted` crossed Kafka and moved a taste vector.

    A list that is not FALLBACK is the assertion: the fallback needs no signals at all, so anything else
    means Order's payload was read, the game was matched, and the ranker ran.
    """
    assert generated["source"] in ("CONTENT", "COLLAB", "HYBRID"), generated
    assert generated["items"], generated
    assert generated["generated_at"] is not None, generated


# --- the ranking itself --------------------------------------------------


def test_an_owned_game_is_never_recommended(generated, catalogue):
    """The one rule that must never break. Suggesting a game back to the person who just bought it is the
    most visible possible failure of a recommender, and it is enforced at ranking rather than by the query
    that found the candidates — so it holds however a candidate got into the running."""
    assert catalogue["racing_owned"] not in ids(generated), generated


def test_a_racing_fan_is_offered_the_other_racing_game(generated, catalogue):
    """The content half, end to end: shared genre beats no shared genre."""
    assert catalogue["racing_similar"] in ids(generated), generated


def test_the_shared_genre_is_reported_as_the_reason(generated, catalogue):
    """A suggestion carries why it was made. This is not decoration — the score is a float nobody can
    interpret, and "because you like racing games" is the only part of the answer a user can check."""
    item = next(i for i in generated["items"] if i["game_id"] == catalogue["racing_similar"])
    assert f"genre:{RACING}" in item["reasons"], item


def test_content_similarity_outranks_a_bare_co_purchase(with_co_purchase, catalogue):
    """The racing game outranks the strategy game.

    Both are candidates and for different reasons: one shares a genre, the other was bought by the same
    neighbour. This is the 65/35 blend expressed as behaviour, and it is the assertion worth having —
    the weights may be retuned, but a racing fan being shown strategy games first would be a regression
    whatever the numbers say.
    """
    order = ids(with_co_purchase)
    assert catalogue["racing_similar"] in order, with_co_purchase
    assert catalogue["strategy"] in order, with_co_purchase
    assert order.index(catalogue["racing_similar"]) < order.index(catalogue["strategy"]), with_co_purchase


def test_the_collaborative_half_finds_a_game_sharing_no_genre(with_co_purchase, catalogue):
    """"People who bought this also bought that", with nothing in common but the buyers.

    The strategy game shares no genre and no tag with anything the racing fan owns. The only path by which
    it can appear at all is the co-purchase edge through the neighbour — so its presence is the item-item
    query working across a real Postgres self-join.
    """
    strategy = next(i for i in with_co_purchase["items"] if i["game_id"] == catalogue["strategy"])
    assert strategy["source"] == "COLLAB", strategy
    assert strategy["reasons"] == [], strategy


def test_ranks_are_dense_and_one_based(generated):
    """Rank is stored rather than derived from a sort at read time, so that two reads of the same list
    cannot order differently when scores tie."""
    assert [item["rank"] for item in generated["items"]] == list(range(1, len(generated["items"]) + 1))


def test_regeneration_is_stable(generated, racing_fan, support):
    """The same signals produce the same list.

    Ties are common at low signal counts and are broken by popularity and then by game id, precisely so a
    user's storefront does not reshuffle every five minutes for no reason.
    """
    before = recommendations_for(racing_fan, support)
    refresh(support)
    again = recommendations_for(racing_fan, support)
    # Both reads are taken here rather than comparing against `generated`. That fixture is session-scoped
    # and resolves as soon as the *content* match lands, so the co-purchase edge legitimately arrives
    # between the two — which is a list that grew, not a list that shuffled.
    assert ids(again) == ids(before), (ids(before), ids(again))


# --- reviews -------------------------------------------------------------


@pytest.mark.slow
def test_a_review_is_ingested_as_a_signal(generated, racing_fan, catalogue, support):
    """Review's `ReviewPosted` reaches the taste vector.

    The racing fan owns the game, which is Review's own precondition for posting. What is asserted is not a
    score — a like on a game already owned reinforces a direction the purchase already set, so the ordering
    need not move — but that the event was accepted and the list still regenerates. A malformed contract
    would surface as a dead-lettered `review-events` message, which `test_99` then fails on.
    """
    posted = a.call(
        "POST",
        f"{REVIEW}/api/reviews/",
        user=racing_fan,
        body={"game_id": catalogue["racing_owned"], "text": "great racing game", "sentiment": "LIKE"},
    )
    if posted.status not in (200, 201):
        pytest.skip(f"review-service refused the review ({posted.status}); not this service's contract")

    refreshed = eventually(
        lambda: _refreshed_list(racing_fan, support),
        message="the list stopped regenerating after a review was posted",
    )
    assert refreshed["items"], refreshed


# --- graceful degradation ------------------------------------------------


def test_an_unknown_user_gets_popular_games_rather_than_nothing(catalogue, support):
    """§ب-۹'s fallback. A user this service has never heard of still gets a list, labelled FALLBACK.

    This is the bulkhead made concrete: the read path has no failure mode that returns an empty list, so a
    storefront can render the section unconditionally instead of branching on whether personalisation
    happened to be available.
    """
    stranger = a.new_id()
    payload = recommendations_for(stranger, support)
    assert payload["source"] == "FALLBACK", payload
    assert payload["items"], "a cold user got nothing at all"
    assert payload["generated_at"] is None, payload
    assert all(item["source"] == "FALLBACK" for item in payload["items"]), payload


def test_the_fallback_still_excludes_what_the_user_owns(racing_fan, catalogue, support):
    """Even the degraded path respects the one rule. A popularity list is allowed to be crude; it is not
    allowed to sell someone a game they already have."""
    payload = recommendations_for(racing_fan, support)
    assert catalogue["racing_owned"] not in ids(payload), payload


def test_similar_games_for_an_unknown_game_is_a_404():
    """Not an empty list. "This game has no neighbours" and "this game does not exist" are different
    answers, and a client that cannot tell them apart cannot report either."""
    response = a.call("GET", f"{RECOMMENDATION}/v1/games/{uuid.uuid4()}/similar")
    assert response.status == 404, response


# --- who may read what ---------------------------------------------------


def test_recommendations_require_a_token():
    """Personalised reads are authenticated. The list is derived from what somebody bought, which makes it
    a statement about a person rather than about the catalogue."""
    assert a.call("GET", f"{RECOMMENDATION}/v1/recommendations").status in (401, 403)


def test_a_user_reads_their_own_recommendations(racing_fan, generated):
    """The route a storefront actually calls, with the user's own token."""
    response = reco("GET", "/v1/recommendations", user=racing_fan)
    assert response.status == 200, response
    assert response.body["user_id"] == racing_fan, response.body


def test_a_user_may_not_read_someone_elses_recommendations(racing_fan, neighbour):
    response = reco("GET", f"/v1/users/{neighbour}/recommendations", user=racing_fan)
    assert response.status == 403, response


def test_support_may_read_anyones_recommendations(racing_fan, support):
    """Support answers "why am I being shown this?", which needs the same list the user sees."""
    response = reco("GET", f"/v1/users/{racing_fan}/recommendations", user=support, role="SUPPORT")
    assert response.status == 200, response


def test_only_support_may_force_a_refresh(racing_fan):
    """The sweep is bounded but not free, and an endpoint that runs it is not one every user may call."""
    assert reco("POST", "/v1/admin/recommendations/refresh", user=racing_fan).status == 403


def test_similar_games_are_public():
    """No token. The answer is a property of the catalogue, which is itself public, so requiring one would
    buy nothing and hide the rail from a logged-out visitor."""
    response = a.call("GET", f"{RECOMMENDATION}/v1/games/{uuid.uuid4()}/similar")
    assert response.status != 401, response


# --- the event it publishes ----------------------------------------------


def test_recommendation_generated_reaches_kafka(generated):
    """The one event this service produces, on its own topic.

    Profile is the consumer. The whole list travels in the event rather than a "come and fetch it"
    notification, so a regeneration for every user does not turn into a call back into this service at the
    moment the batch is under most load.
    """
    # Polled, not asserted outright: the sweep writes the outbox row inside its own transaction and the
    # dispatcher drains on a 500ms tick, so a check made immediately after a refresh is racing the poll
    # rather than testing anything. `test_99` makes the same assertion across every service once the suite
    # has stopped generating work.
    eventually(
        lambda: int(a.psql("recommendation", "SELECT count(*) FROM outbox WHERE published_at IS NULL")) == 0,
        timeout=30.0,
        message="RecommendationGenerated events never reached Kafka",
    )
    assert a.topic_message_count("reco-events") > 0, "reco-events is empty"


def test_no_signal_was_dropped_for_want_of_a_game(generated):
    """Every ownership row was eventually credited to a taste vector.

    `PurchaseCompleted` and `GamePublished` arrive on independent topics, and on a cold start replaying
    history the purchase usually lands first. Those rows are recorded uncounted and credited when the
    description turns up. A row still uncounted after a sweep means that backfill silently stopped working
    — which would not fail anything else here, it would just quietly make every taste vector emptier than
    it should be.
    """
    uncounted = int(a.psql("recommendation", "SELECT count(*) FROM ownerships WHERE NOT counted"))
    assert uncounted == 0, f"{uncounted} purchases never reached a taste vector"
