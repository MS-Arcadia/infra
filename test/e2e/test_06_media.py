"""The media service: what may be stored, and who may read it."""

from __future__ import annotations

import arcadia as a
import pytest


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
ZIP = b"PK\x03\x04" + bytes(range(256)) * 8
HTML = b"<!DOCTYPE html><script>alert(document.cookie)</script>"


@pytest.fixture(scope="session")
def screenshot(developer, game) -> dict:
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("screenshot.png", PNG, "image/png"),
        fields={"kind": "IMAGE", "reference_id": game["id"]},
    )
    assert response.status == 201, response
    return response.body


@pytest.fixture(scope="session")
def build(developer, game) -> dict:
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("game.zip", ZIP, "application/zip"),
        fields={"kind": "GAME_BINARY", "reference_id": game["id"]},
    )
    assert response.status == 201, response
    return response.body


def test_an_image_uploads_as_public(screenshot):
    assert screenshot["content_type"] == "image/png"
    assert screenshot["size_bytes"] == len(PNG)
    assert screenshot["visibility"] == "PUBLIC"
    assert screenshot["url"]


def test_a_public_image_downloads_byte_identical_with_no_token(screenshot):
    response = a.call("GET", screenshot["url"])
    assert response.status == 200
    assert response.raw == PNG
    assert response.headers["content-type"] == "image/png"


def test_a_download_is_an_attachment_and_forbids_sniffing(screenshot):
    """Two layers against an uploaded file executing in the context of our own origin."""
    response = a.call("GET", screenshot["url"])
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_public_asset_is_cacheable_and_a_private_one_is_not(screenshot, build, developer):
    public = a.call("GET", screenshot["url"])
    assert "immutable" in public.headers["cache-control"]

    private = a.call(
        "GET", f"{a.MEDIA}/v1/media/{build['id']}/content", user=developer, role="DEVELOPER"
    )
    assert private.headers["cache-control"] == "private, no-store"


def test_an_html_page_declared_as_a_png_is_refused(developer):
    """Stored and served from our own origin, this is stored cross-site scripting."""
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("evil.png", HTML, "image/png"),
        fields={"kind": "IMAGE"},
    )
    assert response.status == 400
    assert response.body["reason"] == "CONTENT_TYPE_MISMATCH"


def test_an_svg_is_not_an_allowed_image_type(developer):
    """SVG is XML and can carry script."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("x.svg", svg, "image/svg+xml"),
        fields={"kind": "IMAGE"},
    )
    assert response.status == 400


def test_an_empty_upload_is_refused(developer):
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("empty.png", b"", "image/png"),
        fields={"kind": "IMAGE"},
    )
    assert response.status == 400
    assert response.body["reason"] == "MEDIA_EMPTY"


def test_a_game_binary_is_private_with_no_direct_url(build):
    assert build["visibility"] == "PRIVATE"
    assert build["url"] == ""


def test_a_game_binary_cannot_be_uploaded_as_public(developer):
    """An unauthenticated URL for a build is a pirated copy."""
    response = a.multipart(
        f"{a.MEDIA}/v1/media",
        user=developer,
        role="DEVELOPER",
        file=("public.zip", ZIP, "application/zip"),
        fields={"kind": "GAME_BINARY", "visibility": "PUBLIC"},
    )
    assert response.status == 400
    assert response.body["reason"] == "VISIBILITY_NOT_ALLOWED"


def test_a_private_file_is_not_downloadable_anonymously(build):
    """404, not 403 — "forbidden" confirms the id is real."""
    response = a.call("GET", f"{a.MEDIA}/v1/media/{build['id']}/content")
    assert response.status == 404


def test_a_signed_ticket_downloads_a_private_file(build, developer):
    """The local equivalent of an S3 presigned URL: it works in a download manager, which
    would never attach a bearer token."""
    ticket = a.call(
        "POST", f"{a.MEDIA}/v1/media/{build['id']}/ticket", user=developer, role="DEVELOPER"
    )
    assert ticket.status == 200, ticket
    assert ticket.body["expires_in_seconds"] == 900

    download = a.call("GET", ticket.body["url"])
    assert download.status == 200
    assert download.raw == ZIP


def test_a_tampered_ticket_is_refused(build, developer):
    ticket = a.call(
        "POST", f"{a.MEDIA}/v1/media/{build['id']}/ticket", user=developer, role="DEVELOPER"
    )
    forged = ticket.body["url"][:-4] + "AAAA"
    assert a.call("GET", forged).status == 403


def test_a_stranger_cannot_get_a_ticket_for_someone_elses_build(build, stranger):
    response = a.call(
        "POST", f"{a.MEDIA}/v1/media/{build['id']}/ticket", user=stranger
    )
    assert response.status == 404


def test_listing_by_reference_returns_the_screenshot_and_not_the_build(
    screenshot, build, game
):
    """How a storefront page fetches a game's images. It must not leak the build."""
    response = a.call("GET", f"{a.MEDIA}/v1/media/by-reference/{game['id']}")
    assert response.status == 200
    kinds = {item["kind"] for item in response.body}
    assert kinds == {"IMAGE"}, response.body


# --- quotas -------------------------------------------------------------


def test_a_developer_can_see_how_much_of_their_quota_they_have_used(developer, screenshot, build):
    """A quota nobody can see is one they discover by having an upload refused after the bytes
    have already gone over the wire."""
    response = a.call("GET", f"{a.MEDIA}/v1/media/usage", user=developer, role="DEVELOPER")
    assert response.status == 200, response

    body = response.body
    assert body["quota_bytes"] > 0
    assert body["used_bytes"] >= len(PNG) + len(ZIP)
    assert body["used_bytes"] + body["remaining_bytes"] == body["quota_bytes"]


def test_the_quota_is_counted_per_developer(developer, screenshot):
    """A fair share, not a total. Somebody who has uploaded nothing has their whole quota."""
    newcomer = a.new_id()
    theirs = a.call("GET", f"{a.MEDIA}/v1/media/usage", user=newcomer, role="DEVELOPER").body
    mine = a.call("GET", f"{a.MEDIA}/v1/media/usage", user=developer, role="DEVELOPER").body

    assert theirs["used_bytes"] == 0
    assert theirs["remaining_bytes"] == theirs["quota_bytes"]
    assert mine["used_bytes"] > 0


def test_there_is_no_anonymous_quota(developer):
    assert a.call("GET", f"{a.MEDIA}/v1/media/usage").status == 401
