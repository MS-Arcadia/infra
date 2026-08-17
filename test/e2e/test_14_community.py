"""Per-game communities, requirement 1.8 — posts, comments, reactions, moderation.

Numbered 14 so it runs after the games exist, and before `test_99` judges the platform.

Three things here cannot be tested anywhere but against a running stack, which is the whole
reason this file exists:

* **A post needs a published game.** Community asks Catalog, over HTTP, on every create. Its
  own suite answers that question with a fake that always says yes.
* **A post becomes findable through Search.** Community publishes `PostCreated` to
  `community-events`, Search consumes it into a tsvector index, and community's own
  `/v1/posts/search` asks Search for ids and hydrates them locally. Three services and a
  broker for one assertion.
* **Moderation and identity are read from a token minted by something else.** Every rule
  below — author-only edits, Support-only queue, banned-user refusal — is a claim in a JWT
  this service did not issue.

The fixtures deliberately publish their *own* game rather than reusing the session-scoped
`game` from `conftest`. Community caches Catalog's answer for 60 seconds and primes that
cache from the `GamePublished` event, so a game published at the top of the suite may well
have aged out of it by the time this file runs — see `test_the_catalog_contract_holds` for
what happens then, and why that test is here.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import arcadia as a
import jwt
import pytest

# Every address below is the service's own port. The gateway is `a.GATEWAY`, and
# `test_the_gateway_reaches_the_community_service` is the one that compares them.
COMMUNITY = "http://localhost:8091"
SEARCH = "http://localhost:8092"

THUMBS_UP = "\N{THUMBS UP SIGN}"
FIRE = "\N{FIRE}"

# Reaction weight 1, comment weight 2 — community-service's FeedbackScorePolicy, and the
# ranking signal behind Profile's "top 5 posts". Written out here rather than imported
# because this suite is a client of the platform and must not depend on any service's code.
REACTION_WEIGHT = 1
COMMENT_WEIGHT = 2


# --- helpers -------------------------------------------------------------


def community(method: str, path: str, **kwargs) -> a.Response:
    return a.call(method, COMMUNITY + path, **kwargs)


def token(user_id: str, role: str = "BASIC_USER", **claims: object) -> str:
    """A token with claims `arcadia.token` does not offer.

    Only two tests need this — the banned user and the refresh token — and both are about a
    claim the helper deliberately never sets. Everything else uses `user=`.
    """
    payload: dict[str, object] = {
        "sub": user_id,
        "role": role,
        "typ": "access",
        "iss": a.ISSUER,
        "aud": a.AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(hours=2),
    }
    payload.update(claims)
    return jwt.encode(payload, a.JWT_SECRET, algorithm="HS256")


def publish_game(developer: str, support: str, title: str) -> str:
    """A game taken to PUBLISHED, returning its id.

    The same seven steps `conftest.game` runs. Repeated rather than shared because this file
    needs a *recently* published game and that fixture is session-scoped by design.
    """
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": title, "description": "for the community end-to-end tests"},
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


def post_to(game_id: str, author: str, body: str, **kwargs) -> dict:
    response = community(
        "POST", "/v1/posts", user=author, body={"game_id": game_id, "body": body}, **kwargs
    )
    assert response.status == 201, response
    return response.body


def feed_items(game_id: str, *, limit: int, sort: str = "newest", cursor: str | None = None) -> dict:
    """One page of a game feed.

    `limit` doubles as a cache-buster. The feed is read through a 15-second Redis cache keyed
    by (game, sort, page) with no invalidation on write, so two tests that read the same page
    size within that window would see one test's snapshot in the other. Each caller below
    passes a size nobody else uses.
    """
    query = f"?sort={sort}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
    response = community("GET", f"/v1/games/{game_id}/feed{query}")
    assert response.status == 200, response
    return response.body


def find_report(report_id: str, support: str, *, pages: int = 5) -> dict | None:
    """Walk the open moderation queue looking for one report.

    Bounded rather than exhaustive: the queue is platform-wide and every previous run of this
    suite has left reports in it, so "not in the first few pages" is the honest thing to
    assert, and a cursor loop with no bound is how a test hangs on a busy database.
    """
    cursor = None
    for _ in range(pages):
        query = "?status=open&limit=50" + (f"&cursor={cursor}" if cursor else "")
        page = community("GET", f"/v1/moderation/reports{query}", user=support, role="SUPPORT")
        assert page.status == 200, page
        for report in page.body["items"]:
            if report["id"] == report_id:
                return report
        cursor = page.body.get("next_cursor")
        if not cursor:
            return None
    return None


# --- fixtures ------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def community_is_up() -> None:
    """Fail here, once, rather than in whichever test happened to run first.

    `conftest`'s session check covers the seven services that existed when it was written;
    community is not among them, so this file carries its own.
    """
    response = community("GET", "/readyz")
    if response.status != 200:
        pytest.fail(
            f"community-service is not ready: {response.status} {response.body}\n\n"
            f"Start it with:  cd infra && make up && make wait"
        )


@pytest.fixture(scope="module")
def game_id(developer, support) -> str:
    """A published game that community-service will accept posts about.

    The wait is the interesting part. Publication reaches this service two ways: over
    `game-events`, which primes a 60-second cache entry, and over HTTP to Catalog when that
    entry is cold. So this polls until a post is accepted rather than assuming either path.

    Each probe uses a throwaway user id, because the create-post rate limit is ten per minute
    *per user* and a poll on one identity would exhaust it before the event arrived.
    """
    published = publish_game(developer, support, "Neon Drift: Community Cut")
    deadline = time.monotonic() + 45
    last = None
    while time.monotonic() < deadline:
        probe = community(
            "POST", "/v1/posts", user=a.new_id(),
            body={"game_id": published, "body": "checking the community is open"},
        )
        if probe.status == 201:
            return published
        last = probe
        time.sleep(1.0)
    pytest.fail(
        f"community-service would not accept a post about a game Catalog says is published.\n"
        f"Last response: {last}\n\n"
        f"See test_the_catalog_contract_holds — this is what its failure looks like downstream."
    )


@pytest.fixture(scope="module")
def author() -> str:
    """One identity for the posts whose authorship is asserted."""
    return a.new_id()


@pytest.fixture(scope="module")
def post(game_id, author) -> dict:
    return post_to(game_id, author, "the drift physics in chapter three are extraordinary")


# --- the integration seams -----------------------------------------------


def test_the_catalog_contract_holds(game_id):
    """Community asks Catalog `GET /v1/games/{id}` before it accepts any post, and reads
    `state` off the answer.

    The one assertion in the file about a URL rather than a behaviour, and it earns its
    place: a 404 here is indistinguishable from "the game is not published", so the failure
    it causes is a 412 on every post about every game — and only once the 60-second cache
    primed by `GamePublished` has expired, which is why it survived every service's own
    suite. It was `/api/v1/games/{id}` until this test was written, and that path does not
    exist on catalog-service.
    """
    response = a.call("GET", f"{a.CATALOG}/v1/games/{game_id}")
    assert response.status == 200, (
        f"community-service fetches /v1/games/{{id}} from catalog-service and got "
        f"{response.status}. A 404 there is read as 'not published', so posting will fail "
        f"with 412 as soon as the event-primed cache entry expires."
    )
    assert response.body["state"] == "PUBLISHED", (
        f"the adapter reads publication off `state`; catalog answered {response.body.get('state')!r}"
    )


def test_a_post_needs_a_published_game(author):
    """412, not 404: the game id is well-formed and this service is not the authority on it."""
    response = community(
        "POST", "/v1/posts", user=author,
        body={"game_id": str(uuid.uuid4()), "body": "about a game that does not exist"},
    )
    assert response.status == 412, response
    assert response.body["title"] == "FAILED_PRECONDITION"


def test_the_gateway_reaches_the_community_service(game_id):
    """The gateway strips `/community` and forwards the rest. A browser only ever sees this."""
    through = community("GET", f"/v1/games/{game_id}/feed?limit=7")
    direct = a.call("GET", f"{a.GATEWAY}/community/v1/games/{game_id}/feed?limit=7")
    assert direct.status == 200, direct
    assert [item["id"] for item in direct.body["items"]] == [
        item["id"] for item in through.body["items"]
    ]


# --- posts ---------------------------------------------------------------


def test_a_post_appears_in_its_games_feed(game_id, post):
    page = feed_items(game_id, limit=33)
    assert post["id"] in [item["id"] for item in page["items"]]


def test_a_post_is_readable_without_a_login(post):
    """The storefront's community tab must render for someone who has not signed in."""
    response = community("GET", f"/v1/posts/{post['id']}")
    assert response.status == 200, response
    assert response.body["body"] == post["body"]
    assert response.body["status"] == "ACTIVE"


def test_reading_a_post_counts_a_view(game_id, author):
    """The counter the MOST_VIEWED ordering sorts on, and it must include the read that
    returned it rather than lagging one behind."""
    created = post_to(game_id, author, "does anyone else map the brake to a trigger")
    first = community("GET", f"/v1/posts/{created['id']}")
    second = community("GET", f"/v1/posts/{created['id']}")
    assert second.body["view_count"] > first.body["view_count"]


def test_only_the_author_may_edit_a_post(post, author):
    """Enforced by the aggregate, from a `sub` claim in a token minted elsewhere."""
    theirs = community(
        "PATCH", f"/v1/posts/{post['id']}", user=a.new_id(),
        body={"body": "I am editing somebody else's post", "spoiler": False, "tags": []},
    )
    assert theirs.status == 403, theirs

    mine = community(
        "PATCH", f"/v1/posts/{post['id']}", user=author,
        body={"body": post["body"] + " (edited)", "spoiler": True, "tags": ["racing"]},
    )
    assert mine.status == 200, mine
    assert mine.body["spoiler"] is True
    assert mine.body["tags"] == ["racing"]
    assert mine.body["edited_at"] is not None


def test_a_banned_user_may_not_post(game_id):
    """The ban arrives as a `state` claim. Nothing in this service stores it."""
    banned = token(a.new_id(), state="BANNED")
    response = community(
        "POST", "/v1/posts", bearer=banned, body={"game_id": game_id, "body": "let me back in"}
    )
    assert response.status == 403, response
    assert response.body["title"] == "PERMISSION_DENIED"


def test_a_refresh_token_is_not_an_access_token(game_id):
    """A seven-day credential must not work on an endpoint meant for a fifteen-minute one.

    The same check `test_00_identity` makes of the platform's other five services, made here
    because a service that reads `typ` differently is exactly how that bug got in.
    """
    refresh = token(a.new_id(), typ="refresh")
    response = community(
        "POST", "/v1/posts", bearer=refresh, body={"game_id": game_id, "body": "using the wrong token"}
    )
    assert response.status == 401, response


def test_the_feed_pages_by_cursor_without_repeating_a_post(game_id):
    """A total order or nothing: without the (created_at, id) tie-breaker, cursor pagination
    silently skips or duplicates rows, and it does so only under load."""
    writer = a.new_id()
    created = [post_to(game_id, writer, f"cursor probe {index}") for index in range(3)]

    first = feed_items(game_id, limit=2)
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = feed_items(game_id, limit=2, cursor=first["next_cursor"])
    seen = [item["id"] for item in first["items"] + second["items"]]
    assert len(seen) == len(set(seen)), f"a post was served on both pages: {seen}"
    # Newest first, so the three just written lead the feed.
    assert seen[:3] == [entry["id"] for entry in reversed(created)]


def test_the_explore_feed_carries_posts_from_every_game(game_id, post):
    response = community("GET", "/v1/feed/explore?limit=41")
    assert response.status == 200, response
    assert response.body["items"], "the explore feed is empty after this suite wrote to it"


# --- reactions -----------------------------------------------------------


def test_a_reaction_is_idempotent_and_toggles_off(post):
    """PUT, not POST, and the emoji already held clears it. A double tap on a phone must not
    count twice, and must not be an error either."""
    reactor = a.new_id()
    first = community("PUT", f"/v1/posts/{post['id']}/reactions", user=reactor, body={"emoji": THUMBS_UP})
    assert first.status == 200, first
    assert first.body["reactions"][THUMBS_UP] == 1
    assert first.body["my_reaction"] == THUMBS_UP

    again = community("PUT", f"/v1/posts/{post['id']}/reactions", user=reactor, body={"emoji": THUMBS_UP})
    assert again.status == 200, again
    assert again.body["reactions"].get(THUMBS_UP, 0) == 0
    assert again.body["my_reaction"] is None


def test_one_reaction_per_user_and_a_swap_moves_the_count(post):
    reactor = a.new_id()
    community("PUT", f"/v1/posts/{post['id']}/reactions", user=reactor, body={"emoji": THUMBS_UP})
    swapped = community("PUT", f"/v1/posts/{post['id']}/reactions", user=reactor, body={"emoji": FIRE})
    assert swapped.status == 200, swapped
    assert swapped.body["reactions"][FIRE] == 1
    assert swapped.body["reactions"].get(THUMBS_UP, 0) == 0
    assert swapped.body["total"] == 1

    cleared = community("DELETE", f"/v1/posts/{post['id']}/reactions", user=reactor)
    assert cleared.status == 200, cleared
    assert cleared.body["my_reaction"] is None


def test_an_emoji_outside_the_catalog_is_refused(post):
    """The set is closed so the summary has a bounded key space and clients can render a
    fixed picker."""
    response = community(
        "PUT", f"/v1/posts/{post['id']}/reactions", user=a.new_id(), body={"emoji": "\N{PILE OF POO}"}
    )
    assert response.status == 422, response


# --- comments ------------------------------------------------------------


def test_a_comment_raises_the_posts_feedback_score(game_id, author):
    """The score Profile ranks an author's top five by: one point a reaction, two a comment.

    Asserted through the API rather than against the formula, because the interesting part is
    that the comment, the parent's `comment_count` and the re-scored post all commit together.
    """
    subject = post_to(game_id, author, "rate my time trial ghost")
    before = community("GET", f"/v1/posts/{subject['id']}").body["feedback_score"]

    commented = community(
        "POST", f"/v1/posts/{subject['id']}/comments", user=a.new_id(), body={"body": "that is a clean line"}
    )
    assert commented.status == 201, commented
    community("PUT", f"/v1/posts/{subject['id']}/reactions", user=a.new_id(), body={"emoji": FIRE})

    after = community("GET", f"/v1/posts/{subject['id']}")
    assert after.body["comment_count"] == 1
    assert after.body["feedback_score"] == before + COMMENT_WEIGHT + REACTION_WEIGHT


def test_a_repeated_idempotency_key_replays_the_first_comment(post):
    """The key is scoped by method and path and stored in Redis, shared across every replica.

    A duplicate submit on a flaky connection is the most visible failure mode a feed has, and
    this is the only test in the suite that proves community's idempotency store is the real
    one rather than the in-memory fake it runs with by default.
    """
    commenter = a.new_id()
    key = f"e2e-comment-{uuid.uuid4()}"
    body = {"body": "posted once, sent twice"}

    first = community("POST", f"/v1/posts/{post['id']}/comments", user=commenter, key=key, body=body)
    assert first.status == 201, first

    # Still 201: the status is the route's declared one either way, and a replay announces
    # itself with the header rather than by changing it. The identity of the comment is what
    # actually matters here.
    replay = community("POST", f"/v1/posts/{post['id']}/comments", user=commenter, key=key, body=body)
    assert replay.status == 201, replay
    assert replay.body["id"] == first.body["id"], "a second comment was created"
    assert replay.headers.get("idempotency-replayed") == "true"

    listed = community("GET", f"/v1/posts/{post['id']}/comments?limit=50")
    mine = [c for c in listed.body["items"] if c["author_id"] == commenter]
    assert len(mine) == 1, mine


def test_only_the_author_may_edit_a_comment(post):
    commenter = a.new_id()
    created = community(
        "POST", f"/v1/posts/{post['id']}/comments", user=commenter, body={"body": "first impressions"}
    )
    assert created.status == 201, created

    theirs = community(
        "PATCH", f"/v1/comments/{created.body['id']}", user=a.new_id(), body={"body": "not mine to edit"}
    )
    assert theirs.status == 403, theirs

    mine = community(
        "PATCH", f"/v1/comments/{created.body['id']}", user=commenter, body={"body": "second impressions"}
    )
    assert mine.status == 200, mine
    assert mine.body["body"] == "second impressions"


# --- moderation ----------------------------------------------------------


def test_a_basic_user_cannot_read_the_moderation_queue():
    response = community("GET", "/v1/moderation/reports?status=open", user=a.new_id())
    assert response.status == 403, response


def test_reporting_the_same_post_twice_returns_the_original_report(post):
    """A double tap on "report" is not an error and must not open a second ticket for Support."""
    reporter = a.new_id()
    first = community(
        "POST", f"/v1/posts/{post['id']}/reports", user=reporter, body={"reason": "spoilers, unmarked"}
    )
    assert first.status == 201, first
    again = community(
        "POST", f"/v1/posts/{post['id']}/reports", user=reporter, body={"reason": "spoilers, unmarked"}
    )
    assert again.status == 201, again
    assert again.body["id"] == first.body["id"]


def test_support_removes_a_reported_post_and_it_leaves_the_feed(game_id, author, support):
    """The whole moderation loop, across three identities and two aggregates.

    The removal and the report's resolution commit in one transaction, so a resolved report
    can never disagree with the state of the content it was about — asserted here by checking
    both sides after the fact.
    """
    offending = post_to(game_id, author, "here is a full plot summary of the final chapter")
    reported = community(
        "POST", f"/v1/posts/{offending['id']}/reports", user=a.new_id(),
        body={"reason": "unmarked spoilers for the ending"},
    )
    assert reported.status == 201, reported
    report_id = reported.body["id"]
    assert reported.body["status"] == "OPEN"

    assert find_report(report_id, support) is not None, (
        f"report {report_id} is not in the first pages of the open queue Support works from"
    )

    resolved = community(
        "POST", f"/v1/moderation/reports/{report_id}/resolve", user=support, role="SUPPORT",
        body={"action": "REMOVE", "note": "spoiler policy, second offence"},
    )
    assert resolved.status == 200, resolved
    assert resolved.body["status"] == "RESOLVED_REMOVED"
    assert resolved.body["resolved_by"] == support

    # 404 rather than 403: "forbidden" would confirm the post is real and tell an author
    # exactly which of their posts was taken down.
    assert community("GET", f"/v1/posts/{offending['id']}").status == 404
    assert community("GET", f"/v1/posts/{offending['id']}", user=author).status == 404

    # Support still sees it. A moderation outcome that hides the evidence from moderators is
    # not a moderation outcome.
    seen_by_support = community("GET", f"/v1/posts/{offending['id']}", user=support, role="SUPPORT")
    assert seen_by_support.status == 200, seen_by_support
    assert seen_by_support.body["status"] == "REMOVED_BY_MODERATION"

    assert offending["id"] not in [item["id"] for item in feed_items(game_id, limit=37)["items"]]


def test_a_resolved_report_cannot_be_resolved_again(post, support):
    """Terminal once resolved: a second decision is a conflict, not an update."""
    reported = community(
        "POST", f"/v1/posts/{post['id']}/reports", user=a.new_id(), body={"reason": "off topic"}
    )
    assert reported.status == 201, reported
    decision = {"action": "DISMISS", "note": "within the rules"}
    first = community(
        "POST", f"/v1/moderation/reports/{reported.body['id']}/resolve",
        user=support, role="SUPPORT", body=decision,
    )
    assert first.status == 200, first
    second = community(
        "POST", f"/v1/moderation/reports/{reported.body['id']}/resolve",
        user=support, role="SUPPORT", body=decision,
    )
    assert second.status == 409, second


def test_a_moderation_decision_must_explain_itself(post, support):
    """The platform-wide rule: no unexplained moderation, in Catalog's review and here alike."""
    reported = community(
        "POST", f"/v1/posts/{post['id']}/reports", user=a.new_id(), body={"reason": "needs a look"}
    )
    response = community(
        "POST", f"/v1/moderation/reports/{reported.body['id']}/resolve",
        user=support, role="SUPPORT", body={"action": "DISMISS", "note": ""},
    )
    assert response.status == 422, response


# --- what other services read --------------------------------------------


def test_the_top_posts_route_ranks_by_feedback_score(game_id):
    """Profile's top-five read model is event-driven; this route is how it backfills."""
    writer = a.new_id()
    quiet = post_to(game_id, writer, "a post nobody engaged with")
    popular = post_to(game_id, writer, "a post with a comment on it")
    community("POST", f"/v1/posts/{popular['id']}/comments", user=a.new_id(), body={"body": "agreed"})
    community("PUT", f"/v1/posts/{quiet['id']}/reactions", user=a.new_id(), body={"emoji": THUMBS_UP})

    response = community("GET", f"/v1/authors/{writer}/top-posts")
    assert response.status == 200, response
    assert len(response.body) <= 5
    ranked = [item["id"] for item in response.body]
    assert ranked.index(popular["id"]) < ranked.index(quiet["id"]), (
        f"a comment is worth {COMMENT_WEIGHT} and a reaction {REACTION_WEIGHT}, "
        f"so the commented post must rank first: {ranked}"
    )


@pytest.mark.slow
def test_a_post_becomes_findable_through_the_search_service(game_id, author):
    """Community → `community-events` → Search's index → back through community's own search.

    The strongest assertion in the file: two services, a broker and a tsvector index, for one
    HTTP call. Nothing short of a running platform can make it.

    The needle is a word that exists nowhere else, so a hit cannot be a coincidence and the
    test stays repeatable without resetting the index.
    """
    needle = f"driftmarker{uuid.uuid4().hex[:10]}"
    indexed = post_to(game_id, author, f"the {needle} corner is the hardest in the game")

    deadline = time.monotonic() + 45
    last = None
    while time.monotonic() < deadline:
        response = community("GET", f"/v1/posts/search?q={needle}&limit=10")
        last = response
        if response.status == 200 and indexed["id"] in [item["id"] for item in response.body["items"]]:
            # Community hydrates bodies locally; Search only ever returns ids.
            found = next(item for item in response.body["items"] if item["id"] == indexed["id"])
            assert needle in found["body"]
            return
        time.sleep(1.0)

    direct = a.call("GET", f"{SEARCH}/api/v1/search/posts?q={needle}&limit=10")
    pytest.fail(
        f"post {indexed['id']} never became searchable.\n"
        f"through community-service: {last}\n"
        f"straight to search-service: {direct}\n\n"
        f"If Search has the id and community does not, the hydration step is at fault; if "
        f"neither does, check the community-events consumer:\n"
        f"  docker exec arcadia-kafka kafka-consumer-groups.sh --bootstrap-server "
        f"localhost:9092 --describe --group search-service"
    )


@pytest.mark.slow
def test_the_community_outbox_drains(post):
    """Every event this service wrote has reached the broker.

    The outbox row and the aggregate commit together, which is the entire reliability
    guarantee — but a row that is never dispatched is a `PostCreated` that Search and Profile
    never see, and no API anywhere reports that. Hence the direct read.
    """
    deadline = time.monotonic() + 20
    unpublished = None
    while time.monotonic() < deadline:
        unpublished = int(a.psql("community", "SELECT count(*) FROM outbox WHERE published_at IS NULL"))
        if unpublished == 0:
            break
        time.sleep(1.0)

    assert unpublished == 0, f"{unpublished} community event(s) never reached Kafka"
    dead = int(a.psql("community", "SELECT count(*) FROM outbox WHERE dead_lettered = true"))
    assert dead == 0, f"{dead} community event(s) were dead-lettered"
