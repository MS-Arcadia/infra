"""The publishing workflow, requirement 1.3.

Runs first, and leaves a published game behind for the rest of the suite. The ordering is
deliberate: everything downstream needs something to sell.
"""

from __future__ import annotations

import arcadia as a
from conftest import PRICE, SUGGESTED_PRICE

def test_a_game_starts_as_a_draft(developer):
    response = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        user=developer,
        role="DEVELOPER",
        body={"title": "A Draft", "description": "not going anywhere"},
    )
    assert response.status == 201
    assert response.body["state"] == "DRAFT"


def test_a_game_with_no_build_cannot_be_submitted(developer):
    created = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        user=developer,
        role="DEVELOPER",
        body={"title": "No Build", "description": "nothing to review"},
    )
    response = a.call(
        "POST",
        f"{a.CATALOG}/v1/games/{created.body['id']}/submit",
        user=developer,
        role="DEVELOPER",
    )
    assert response.status == 422
    assert response.body["reason"] == "GAME_HAS_NO_VERSION"


def test_a_rejection_must_explain_itself(developer, support):
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": "To Be Rejected", "description": "will be refused"},
    )
    game_id = created.body["id"]
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/versions", user=developer, role="DEVELOPER",
           body={"version": "1.0.0", "file_ref": "x", "size_bytes": 1})
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/submit", user=developer, role="DEVELOPER")
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/review/start", user=support, role="SUPPORT")

    response = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/review/reject",
        user=support, role="SUPPORT", body={"note": "   "},
    )
    assert response.status == 400
    assert response.body["reason"] == "REVIEW_NOTE_REQUIRED"


def test_a_rejected_game_can_be_appealed(developer, support):
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": "Appealed", "description": "contested"},
    )
    game_id = created.body["id"]
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/versions", user=developer, role="DEVELOPER",
           body={"version": "1.0.0", "file_ref": "x", "size_bytes": 1})
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/submit", user=developer, role="DEVELOPER")
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/review/start", user=support, role="SUPPORT")
    a.call("POST", f"{a.CATALOG}/v1/games/{game_id}/review/reject", user=support, role="SUPPORT",
           body={"note": "the tutorial crashes"})

    response = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game_id}/appeal",
        user=developer, role="DEVELOPER", body={"note": "fixed in 1.0.1"},
    )
    assert response.status == 200
    assert response.body["state"] == "APPEALED"
    # The original rejection is still on the record, with its note.
    assert response.body["reviews"][0]["note"] == "the tutorial crashes"
    assert response.body["reviews"][0]["appealed"] is True


def test_the_developer_sets_the_price_not_support(game):
    """Requirement 1.3: Support proposes, the developer decides."""
    assert game["suggested_price"]["amount_minor"] == str(SUGGESTED_PRICE)
    assert game["final_price"]["amount_minor"] == str(PRICE)
    assert game["state"] == "PUBLISHED"


def test_support_cannot_publish(game, support):
    """§6.4's diagram gives publishing to Support; requirement 1.3 gives it to the
    developer, and the requirements were finalised later."""
    response = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game['id']}/publish", user=support, role="SUPPORT"
    )
    assert response.status == 403


def test_a_published_game_is_visible_without_a_login(game):
    """The storefront must be browsable by someone who has not signed up yet."""
    response = a.call("GET", f"{a.CATALOG}/v1/games/{game['id']}")
    assert response.status == 200
    assert response.body["title"] == "Neon Drift"


def test_an_unpublished_game_is_reported_as_not_found(developer):
    """404, not 403: "forbidden" would confirm the id is real, which leaks an unannounced
    title to anyone enumerating ids."""
    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", user=developer, role="DEVELOPER",
        body={"title": "Unannounced", "description": "secret"},
    )
    response = a.call("GET", f"{a.CATALOG}/v1/games/{created.body['id']}")
    assert response.status == 404


def test_a_published_game_accepts_a_patch_without_returning_to_review(game, developer):
    """Requirement 1.3. A game must not leave the store to ship its own bugfix."""
    response = a.call(
        "POST", f"{a.CATALOG}/v1/games/{game['id']}/versions",
        user=developer, role="DEVELOPER",
        body={"version": "1.0.1", "file_ref": "patched", "size_bytes": 4096},
    )
    assert response.status == 201
    assert response.body["state"] == "PUBLISHED"
    assert len(response.body["versions"]) == 2
