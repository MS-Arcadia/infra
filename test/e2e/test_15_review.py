"""Reviews, requirement 1.7 — post, edit, delete, react, report, and Support moderation.
"""

from __future__ import annotations

import time

import arcadia as a
import pytest

REVIEW = "http://localhost:8088"


def review(method: str, path: str, **kwargs) -> a.Response:
    return a.call(method, REVIEW + path, **kwargs)


# --- helpers ---------------------------------------------------------------


def publish_game(developer: str, support: str, title: str) -> str:
    """A game taken to PUBLISHED, returning its id.

    The same seven steps `conftest.game` runs, repeated here rather than shared — this
    file needs a fresh game per test, and that fixture is session-scoped by design.
    """
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": title, "description": "for the review end-to-end tests"},
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
        ("POST", f"/v1/games/{game_id}/price/reject", {"amount_minor": 1_000_000}, "DEVELOPER", developer),
        ("POST", f"/v1/games/{game_id}/publish", None, "SUPPORT", support),
    ]
    for method, path, body, role, user in workflow:
        step = a.call(method, f"{a.CATALOG}{path}", user=user, role=role, body=body)
        assert step.status in (200, 201), (path, step)
    return game_id


def own_game(game_id: str, admin: str) -> str:
    """A fresh user, funded and walked through a real purchase of `game_id`.

    Goes through wallet-service and order-service rather than writing to catalog's
    ownership table directly, so what review-service checks is the same entitlement the
    rest of the platform granted — the whole point of testing this against a running
    stack instead of `MockOwnershipChecker`.
    """
    buyer = a.new_id()
    a.provision_wallet(buyer)

    funded = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"review-e2e-fund-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "review end-to-end suite",
        },
    )
    assert funded.status == 200, funded

    order = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=buyer,
        key=f"review-e2e-buy-{buyer}", body={"game_id": game_id},
    )
    assert order.status == 202, order

    deadline = time.monotonic() + 45
    state, order_id = order.body["state"], order.body["id"]
    while state == "PENDING" and time.monotonic() < deadline:
        time.sleep(0.5)
        check = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=buyer)
        assert check.status == 200, check
        state = check.body["state"]
    assert state == "COMPLETED", f"purchase for {buyer} never completed: last state {state}"
    return buyer


def own_game_with_order(game_id: str, admin: str) -> tuple[str, str]:
    """Like `own_game`, but also returns the order id — needed only by the refund test
    below, and kept separate so every other fixture stays with the simpler one-value
    helper it actually needs."""
    buyer = a.new_id()
    a.provision_wallet(buyer)

    funded = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer}/adjust", user=admin, role="ADMIN",
        key=f"review-e2e-fund-{buyer}",
        body={
            "direction": "CREDIT",
            "amount": {"amount_minor": "5000000", "currency": "IRR"},
            "reason": "review end-to-end suite",
        },
    )
    assert funded.status == 200, funded

    order = a.call(
        "POST", f"{a.ORDER}/v1/orders", user=buyer,
        key=f"review-e2e-buy-{buyer}", body={"game_id": game_id},
    )
    assert order.status == 202, order

    deadline = time.monotonic() + 45
    state, order_id = order.body["state"], order.body["id"]
    while state == "PENDING" and time.monotonic() < deadline:
        time.sleep(0.5)
        check = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=buyer)
        assert check.status == 200, check
        state = check.body["state"]
    assert state == "COMPLETED", f"purchase for {buyer} never completed: last state {state}"
    return buyer, order_id


def find_report_id(review_id: str, reporter_id: str) -> str:
    """The report's id, read straight from the database.

    Nothing above `POST /{review_id}/report` returns one — the response is just
    `{"message": "..."}` — so resolving a report from this suite has no other way in.
    """
    row = a.psql(
        "review",
        f"SELECT id FROM review_reports WHERE review_id = '{review_id}' "
        f"AND reporter_id = '{reporter_id}' ORDER BY created_at DESC LIMIT 1",
    )
    assert row, f"no report row for review {review_id} from reporter {reporter_id}"
    return row


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def review_is_up() -> None:
    """Fail here, once, rather than in whichever test happened to run first.

    `conftest`'s session check covers the services that existed when it was written;
    review-service is not among them, so this file carries its own — same as
    `test_14_community.py`.
    """
    response = review("GET", "/readyz")
    if response.status != 200:
        pytest.fail(
            f"review-service is not ready: {response.status} {response.body}\n\n"
            f"Start it with:  cd infra && make up && make wait"
        )


@pytest.fixture(scope="module")
def game_id(developer, support) -> str:
    """One published game the whole file reviews."""
    return publish_game(developer, support, "Neon Drift: Reviewer's Cut")


@pytest.fixture
def owner(game_id, admin) -> str:
    """A fresh user who owns `game_id`, entitled to review it. Bought per test — see the
    module docstring for why a shared purchase would corrupt the counts other tests assert.
    """
    return own_game(game_id, admin)


@pytest.fixture
def posted_review(owner, game_id) -> dict:
    response = review(
        "POST", "/api/reviews/", user=owner,
        body={
            "game_id": game_id,
            "text": "Solid racer, tight controls, one save-corrupting bug.",
            "sentiment": "LIKE",
        },
    )
    assert response.status == 201, response
    return response.body


# --------------------------------------------------------------- posting a review


def test_only_an_owner_may_post_a_review(game_id):
    """The synchronous call to catalog's entitlement endpoint, proven against the real
    service rather than `MockOwnershipChecker`."""
    stranger = a.new_id()  # never bought anything
    response = review(
        "POST", "/api/reviews/", user=stranger,
        body={"game_id": game_id, "text": "never played it", "sentiment": "LIKE"},
    )
    assert response.status == 403, response


def test_an_owner_can_post_a_review(owner, game_id):
    response = review(
        "POST", "/api/reviews/", user=owner,
        body={"game_id": game_id, "text": "Great soundtrack, tight handling.", "sentiment": "LIKE"},
    )
    assert response.status == 201, response
    assert response.body["author_id"] == owner
    assert response.body["game_id"] == game_id
    assert response.body["sentiment"] == "LIKE"
    assert response.body["status"] == "ACTIVE"
    assert response.body["like_count"] == 0
    assert response.body["dislike_count"] == 0
    assert response.body["edited_at"] is None


@pytest.mark.slow
def test_a_refunded_owner_may_still_review(game_id, admin):
    """Requirement 1.7 explicitly: `ever_owned`, not `active`. catalog's own comment on
    `EntitlementView` says review-service needs this because a refund must not silence a
    review someone earned by having actually played the game.

    Follows `test_03_refund.py`'s own contract: refund, then poll the order until the
    wallet confirms it as REFUNDED, rather than assuming the synchronous response means
    the reversal already landed.
    """
    buyer, order_id = own_game_with_order(game_id, admin)

    refund = a.call(
        "POST", f"{a.ORDER}/v1/orders/{order_id}/refund", user=buyer,
        key=f"review-e2e-refund-{buyer}",
    )
    assert refund.status == 200, refund
    assert refund.body["state"] == "REFUNDING"

    deadline = time.monotonic() + 30
    order = refund.body
    while order["state"] != "REFUNDED" and time.monotonic() < deadline:
        time.sleep(0.5)
        order = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", user=buyer).body
    assert order["state"] == "REFUNDED", order

    response = review(
        "POST", "/api/reviews/", user=buyer,
        body={"game_id": game_id, "text": "refunded, but I did play it first", "sentiment": "DISLIKE"},
    )
    assert response.status == 201, response


def test_whitespace_only_text_is_rejected(owner, game_id):
    response = review(
        "POST", "/api/reviews/", user=owner,
        body={"game_id": game_id, "text": "   ", "sentiment": "LIKE"},
    )
    assert response.status == 400, response


def test_posting_a_review_with_no_token_is_refused(game_id):
    """`HTTPBearer()`'s own refusal, not `get_current_user`'s — FastAPI answers a missing
    Authorization header with 403, not 401. `test_11_gateway.py` covers the 401 case, where
    a token is present but malformed; this is the other half."""
    response = review(
        "POST", "/api/reviews/", body={"game_id": game_id, "text": "anonymous", "sentiment": "LIKE"},
    )
    assert response.status == 403, response


def test_posting_a_review_with_a_malformed_token_is_refused(game_id):
    response = review(
        "POST", "/api/reviews/", bearer="not-a-jwt-at-all",
        body={"game_id": game_id, "text": "forged", "sentiment": "LIKE"},
    )
    assert response.status == 401, response


# --------------------------------------------------------------------- editing


def test_only_the_author_may_edit_their_review(posted_review, owner):
    stranger = a.new_id()
    refused = review(
        "PUT", f"/api/reviews/{posted_review['id']}", user=stranger, body={"text": "hijacked"}
    )
    assert refused.status == 403, refused

    allowed = review(
        "PUT", f"/api/reviews/{posted_review['id']}", user=owner,
        body={"text": "Actually even better on replay.", "sentiment": "LIKE"},
    )
    assert allowed.status == 200, allowed
    assert allowed.body["text"] == "Actually even better on replay."
    assert allowed.body["status"] == "EDITED"
    assert allowed.body["edited_at"] is not None


def test_editing_a_nonexistent_review_is_not_found(owner):
    response = review(
        "PUT", f"/api/reviews/{a.new_id()}", user=owner, body={"text": "ghost review"}
    )
    assert response.status == 404, response
    assert response.body["reason"] == "NOT_FOUND", response


# -------------------------------------------------------------------- deleting


def test_only_the_author_may_delete_their_review(posted_review, owner):
    stranger = a.new_id()
    refused = review("DELETE", f"/api/reviews/{posted_review['id']}", user=stranger)
    assert refused.status == 403, refused

    allowed = review("DELETE", f"/api/reviews/{posted_review['id']}", user=owner)
    assert allowed.status == 204, allowed


def test_a_deleted_review_leaves_the_games_list(posted_review, owner, game_id):
    deleted = review("DELETE", f"/api/reviews/{posted_review['id']}", user=owner)
    assert deleted.status == 204, deleted

    listing = review("GET", f"/api/reviews/game/{game_id}")
    assert posted_review["id"] not in [r["id"] for r in listing.body["reviews"]]


def test_a_deleted_review_cannot_be_edited_again(posted_review, owner):
    review("DELETE", f"/api/reviews/{posted_review['id']}", user=owner)
    again = review(
        "PUT", f"/api/reviews/{posted_review['id']}", user=owner, body={"text": "resurrecting it"}
    )
    assert again.status == 409, again
    assert again.body["reason"] == "REVIEW_ALREADY_DELETED", again


def test_a_deleted_review_cannot_be_reported(posted_review, owner):
    review("DELETE", f"/api/reviews/{posted_review['id']}", user=owner)
    reported = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=a.new_id(), body={"reason": "spam"}
    )
    assert reported.status == 409, reported
    assert reported.body["reason"] == "REVIEW_ALREADY_DELETED", reported


# ------------------------------------------------------------------ reacting


def test_the_author_cannot_react_to_their_own_review(posted_review, owner):
    response = review(
        "POST", f"/api/reviews/{posted_review['id']}/react", user=owner, body={"reaction_type": "LIKE"}
    )
    assert response.status == 400, response
    assert response.body["reason"] == "OWN_REVIEW_NOT_ALLOWED", response


def test_another_user_can_like_a_review_and_the_count_moves(posted_review):
    liker = a.new_id()
    response = review(
        "POST", f"/api/reviews/{posted_review['id']}/react", user=liker, body={"reaction_type": "LIKE"}
    )
    assert response.status == 200, response

    listing = review("GET", f"/api/reviews/game/{posted_review['game_id']}")
    mine = next(r for r in listing.body["reviews"] if r["id"] == posted_review["id"])
    assert mine["like_count"] == 1


def test_a_dislike_reaction_moves_the_dislike_count_not_the_like_count(posted_review):
    disliker = a.new_id()
    response = review(
        "POST", f"/api/reviews/{posted_review['id']}/react", user=disliker,
        body={"reaction_type": "DISLIKE"},
    )
    assert response.status == 200, response

    listing = review("GET", f"/api/reviews/game/{posted_review['game_id']}")
    mine = next(r for r in listing.body["reviews"] if r["id"] == posted_review["id"])
    assert mine["dislike_count"] == 1
    assert mine["like_count"] == 0


# ------------------------------------------------------------------ reporting


def test_the_author_cannot_report_their_own_review(posted_review, owner):
    response = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=owner, body={"reason": "self-report"}
    )
    assert response.status == 400, response
    assert response.body["reason"] == "OWN_REVIEW_NOT_ALLOWED", response


def test_a_review_can_be_reported_by_someone_else(posted_review):
    response = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=a.new_id(),
        body={"reason": "contains unmarked spoilers"},
    )
    assert response.status == 201, response
    assert "reported" in response.body["message"].lower()


# --------------------------------------------------------- Support moderation
#
# Every test below mints a SUPPORT (or ADMIN) token with `arcadia.token()`, the same
# helper every other file in this suite uses — a single "role" claim, not a "roles" list.
# review-service's `require_staff` reads `current_user["roles"]`, and
# `src/interfaces/middleware/auth.py`'s `get_current_user` builds that field from
# `payload.get("roles", [])` — a claim this platform's tokens never carry. If
# `test_support_can_resolve_a_report` fails with 403 ROLE_REQUIRED, this mismatch is why:
# every token looks role-less to this one endpoint, for every role. The fix is one line,
# reading `payload.get("role")` into a single-element list the way the rest of the
# platform's staff checks expect.


def test_a_basic_user_cannot_resolve_a_report(posted_review):
    reporter = a.new_id()
    reported = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=reporter, body={"reason": "off-topic"}
    )
    assert reported.status == 201, reported
    report_id = find_report_id(posted_review["id"], reporter)

    refused = review(
        "POST", f"/api/reviews/{posted_review['id']}/reports/{report_id}/resolve",
        user=a.new_id(), role="BASIC_USER",
    )
    assert refused.status == 403, refused
    assert refused.body["reason"] == "ROLE_REQUIRED", refused


def test_support_can_resolve_a_report(posted_review, support):
    """Requirement 1.7: Support reviews a report and resolves it. See the note above this
    section if this fails with ROLE_REQUIRED for a SUPPORT token."""
    reporter = a.new_id()
    reported = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=reporter, body={"reason": "harassment"}
    )
    assert reported.status == 201, reported
    report_id = find_report_id(posted_review["id"], reporter)

    resolved = review(
        "POST", f"/api/reviews/{posted_review['id']}/reports/{report_id}/resolve",
        user=support, role="SUPPORT",
    )
    assert resolved.status == 200, resolved
    assert resolved.body["id"] == posted_review["id"]


def test_a_resolved_report_cannot_be_resolved_again(posted_review, support):
    reporter = a.new_id()
    reported = review(
        "POST", f"/api/reviews/{posted_review['id']}/report", user=reporter, body={"reason": "duplicate"}
    )
    assert reported.status == 201, reported
    report_id = find_report_id(posted_review["id"], reporter)

    first = review(
        "POST", f"/api/reviews/{posted_review['id']}/reports/{report_id}/resolve",
        user=support, role="SUPPORT",
    )
    assert first.status == 200, first

    second = review(
        "POST", f"/api/reviews/{posted_review['id']}/reports/{report_id}/resolve",
        user=support, role="SUPPORT",
    )
    assert second.status == 409, second
    assert second.body["reason"] == "REPORT_ALREADY_RESOLVED", second


def test_support_can_resolve_a_report_by_deleting_the_review(owner, game_id, support):
    """`?delete_review=true` — the removal path requirement 1.7 gives Support for a report
    that turns out to be justified."""
    created = review(
        "POST", "/api/reviews/", user=owner,
        body={"game_id": game_id, "text": "will be removed for abusive language", "sentiment": "DISLIKE"},
    )
    assert created.status == 201, created
    review_id = created.body["id"]

    reporter = a.new_id()
    reported = review(
        "POST", f"/api/reviews/{review_id}/report", user=reporter, body={"reason": "abusive language"}
    )
    assert reported.status == 201, reported
    report_id = find_report_id(review_id, reporter)

    resolved = review(
        "POST", f"/api/reviews/{review_id}/reports/{report_id}/resolve?delete_review=true",
        user=support, role="SUPPORT",
    )
    assert resolved.status == 200, resolved
    assert resolved.body["status"] == "DELETED", resolved

    listing = review("GET", f"/api/reviews/game/{game_id}")
    assert review_id not in [r["id"] for r in listing.body["reviews"]]


# --------------------------------------------------------- listing and rating


def test_the_average_rating_reflects_likes_and_dislikes(developer, support, admin):
    rating_game = publish_game(developer, support, "Neon Drift: Ratings Probe")
    sentiments = ["LIKE", "LIKE", "DISLIKE"]
    for i, sentiment in enumerate(sentiments):
        reviewer = own_game(rating_game, admin)
        response = review(
            "POST", "/api/reviews/", user=reviewer,
            body={"game_id": rating_game, "text": f"review {i + 1}, {sentiment.lower()}d it",
                  "sentiment": sentiment},
        )
        assert response.status == 201, response

    rating = review("GET", f"/api/reviews/game/{rating_game}/rating")
    assert rating.status == 200, rating
    assert rating.body["game_id"] == rating_game
    assert rating.body["total_reviews"] == 3
    assert rating.body["likes"] == 2
    assert rating.body["dislikes"] == 1
    assert rating.body["average_rating"] == pytest.approx(2 / 3)


def test_a_game_with_no_reviews_has_a_null_average(developer, support):
    untouched = publish_game(developer, support, "Neon Drift: Untouched")
    rating = review("GET", f"/api/reviews/game/{untouched}/rating")
    assert rating.status == 200, rating
    assert rating.body["total_reviews"] == 0
    assert rating.body["likes"] == 0
    assert rating.body["dislikes"] == 0
    assert rating.body["average_rating"] is None


def test_the_game_review_list_can_be_paginated(developer, support, admin):
    paged_game = publish_game(developer, support, "Neon Drift: Pagination Probe")
    for i in range(3):
        reviewer = own_game(paged_game, admin)
        response = review(
            "POST", "/api/reviews/", user=reviewer,
            body={"game_id": paged_game, "text": f"review number {i + 1}", "sentiment": "LIKE"},
        )
        assert response.status == 201, response

    first_page = review("GET", f"/api/reviews/game/{paged_game}?limit=2&offset=0")
    assert first_page.status == 200, first_page
    assert len(first_page.body["reviews"]) == 2
    assert first_page.body["page"] == 1
    assert first_page.body["page_size"] == 2

    second_page = review("GET", f"/api/reviews/game/{paged_game}?limit=2&offset=2")
    assert len(second_page.body["reviews"]) == 1


def test_reviews_can_be_sorted_by_like_count(developer, support, admin):
    sorted_game = publish_game(developer, support, "Neon Drift: Sorting Probe")
    quiet_author, popular_author = own_game(sorted_game, admin), own_game(sorted_game, admin)

    quiet = review(
        "POST", "/api/reviews/", user=quiet_author,
        body={"game_id": sorted_game, "text": "nobody reacted to this one", "sentiment": "LIKE"},
    ).body
    popular = review(
        "POST", "/api/reviews/", user=popular_author,
        body={"game_id": sorted_game, "text": "everybody liked this one", "sentiment": "LIKE"},
    ).body
    for _ in range(3):
        response = review(
            "POST", f"/api/reviews/{popular['id']}/react", user=a.new_id(), body={"reaction_type": "LIKE"}
        )
        assert response.status == 200, response

    ranked = review("GET", f"/api/reviews/game/{sorted_game}?sort_by=like_count&sort_order=desc")
    assert ranked.status == 200, ranked
    ids = [r["id"] for r in ranked.body["reviews"]]
    assert ids.index(popular["id"]) < ids.index(quiet["id"]), (
        "the review with three likes must rank above the review with none"
    )


def test_the_game_review_list_needs_no_login(posted_review, game_id):
    """A storefront visitor who has not signed in must still be able to read reviews."""
    response = review("GET", f"/api/reviews/game/{game_id}")
    assert response.status == 200, response
    assert any(r["id"] == posted_review["id"] for r in response.body["reviews"])


# ------------------------------------------------------------------- the gateway


def test_the_review_service_is_reachable_through_the_gateway(posted_review):
    """`test_11_gateway.py`'s routing table already lists `/reviews` -> review-service;
    this is the check that the route actually answers rather than 503ing."""
    through = a.call("GET", f"{a.GATEWAY}/reviews/api/reviews/game/{posted_review['game_id']}")
    direct = review("GET", f"/api/reviews/game/{posted_review['game_id']}")

    assert through.status == direct.status == 200
    assert through.body == direct.body


# ---------------------------------------------------- what the outbox and Kafka show


@pytest.mark.slow
def test_the_review_outbox_drains(posted_review):
    """The outbox row and the review commit together; a row stuck PENDING is an event
    ReviewPosted that never reached Kafka, and no API here reports that."""
    deadline = time.monotonic() + 20
    unsent = None
    while time.monotonic() < deadline:
        unsent = int(a.psql("review", "SELECT count(*) FROM outbox WHERE status <> 'SENT'"))
        if unsent == 0:
            break
        time.sleep(1.0)
    assert unsent == 0, f"{unsent} review event(s) never reached Kafka"


def test_no_review_event_was_dead_lettered():
    depth = a.topic_message_count("review-events.dlq")
    assert depth == 0, f"review-events.dlq holds {depth} message(s) a consumer could not read"
