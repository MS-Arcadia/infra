"""Generate the store Postman collection.

Written as a generator rather than by hand because the collection is 55 endpoints of deeply
nested JSON, and hand-editing that is how a URL ends up disagreeing with its own path array.
The token-minting script is lifted verbatim from the wallet collection so the two behave
identically.
"""

from __future__ import annotations

import json
import pathlib

# Two levels up from infra/api/postman.
ROOT = pathlib.Path(__file__).resolve().parents[3]
WALLET = ROOT / "wallet-service/api/postman/arcadia-wallet.postman_collection.json"
OUT_DIR = pathlib.Path(__file__).resolve().parent

wallet = json.loads(WALLET.read_text())
MINT_SCRIPT = wallet["item"][0]["item"][0]["event"][0]["script"]["exec"]


def url(raw: str) -> dict:
    """Build the url object, deriving `path` from `raw` so the two cannot disagree."""
    base, _, rest = raw.partition("/")
    assert base.startswith("{{"), raw
    path_part, _, query = rest.partition("?")
    obj = {
        "raw": raw,
        "host": [base],
        "path": [segment for segment in path_part.split("/") if segment],
    }
    if query:
        obj["query"] = [
            {"key": k, "value": v}
            for k, _, v in (pair.partition("=") for pair in query.split("&"))
        ]
    return obj


def req(
    name: str,
    method: str,
    raw_url: str,
    *,
    description: str,
    body: dict | str | None = None,
    token: str = "userToken",
    idempotent: bool = False,
    tests: list[str] | None = None,
    form: list[dict] | None = None,
) -> dict:
    headers = []
    if body is not None:
        headers.append({"key": "Content-Type", "value": "application/json"})
    if idempotent:
        headers.append({"key": "Idempotency-Key", "value": "{{idempotencyKey}}"})

    request: dict = {
        "method": method,
        "header": headers,
        "url": url(raw_url),
        "description": description,
    }
    if token != "userToken":
        request["auth"] = {
            "type": "bearer",
            "bearer": [{"key": "token", "value": f"{{{{{token}}}}}", "type": "string"}],
        }
    elif token == "":
        request["auth"] = {"type": "noauth"}

    if form is not None:
        request["body"] = {"mode": "formdata", "formdata": form}
        request["header"] = [h for h in headers if h["key"] != "Content-Type"]
    elif body is not None:
        request["body"] = {
            "mode": "raw",
            "raw": body if isinstance(body, str) else json.dumps(body, indent=2),
        }

    item: dict = {"name": name, "request": request}
    if tests:
        item["event"] = [
            {"listen": "test", "script": {"type": "text/javascript", "exec": tests}}
        ]
    return item


def anon(
    name: str,
    method: str,
    raw_url: str,
    *,
    description: str,
    tests: list[str] | None = None,
) -> dict:
    item = req(name, method, raw_url, description=description, tests=tests)
    item["request"]["auth"] = {"type": "noauth"}
    return item


def folder(name: str, description: str, items: list[dict]) -> dict:
    return {"name": name, "description": description, "item": items}


def ok(code: int) -> list[str]:
    return [f"pm.test('status is {code}', () => pm.response.to.have.status({code}));"]


# Waiting for a saga, in a sandbox with no sleep. The request re-queues itself until the
# order settles, bounded so a genuinely stuck order fails the run instead of hanging it.
def poll_order(*until: str, then: list[str] | None = None) -> list[str]:
    """Re-queue this request until the order reaches one of `until`, then assert `then`.

    Two things make this less obvious than it looks.

    The target is explicit rather than "any settled state", because a pre-order passes through
    RESERVED on its way to COMPLETED — a generic wait would stop at the reservation and then
    assert, correctly, that RESERVED is not COMPLETED.

    The follow-up assertions run **only on the pass that stops the loop.** Running them every
    time reports a failure per attempt, so a wait that succeeded on the fourteenth try still
    ends the run with thirteen red assertions and nothing to say they were expected. That is
    what the first version of this did, and the output was indistinguishable from a genuinely
    broken release.

    Newman needs `--delay-request` for the retries to be spaced at all; see the README.
    """
    targets = ", ".join(f"'{state}'" for state in until)
    lines = [
        "pm.test('status is 200', () => pm.response.to.have.status(200));",
        "const body = pm.response.json();",
        f"const until = [{targets}];",
        "const attempts = Number(pm.collectionVariables.get('pollAttempts') || 0);",
        "const waiting = !until.includes(body.state) && attempts < 40;",
        "if (waiting) {",
        "    pm.collectionVariables.set('pollAttempts', attempts + 1);",
        "    pm.execution.setNextRequest(pm.info.requestName);",
        "} else {",
        "    pm.collectionVariables.set('pollAttempts', 0);",
        "}",
    ]
    if then:
        lines.append("if (!waiting) {")
        lines.extend("    " + line for line in then)
        lines.append("}")
    return lines


def capture(code: int, var: str, field: str = "id") -> list[str]:
    """Capture a field of the response into a collection variable.

    `field` is a JavaScript expression on `body`, not just a key, because several catalog
    endpoints answer with the **whole game** rather than the thing you just created. Adding a
    version returns a GameDetailView, so `body.id` is the game's id — which is exactly how
    `versionId` and `promotionId` both came to hold a game id and every request using them
    returned 404.
    """
    return [
        f"pm.test('status is {code}', () => pm.response.to.have.status({code}));",
        "const body = pm.response.json();",
        f"pm.collectionVariables.set('{var}', body.{field});",
        f"pm.test('{var} was captured', function () {{",
        f"    pm.expect(pm.collectionVariables.get('{var}')).to.be.a('string').and.not.empty;",
        "});",
    ]


def approval_chain(
    var: str, title: str, price_minor: int, *, publish: bool
) -> list[dict]:
    """The six requests that take a game from nothing to for-sale.

    Emitted rather than written out because folders 5 and 6 each need their **own** game: a game
    cannot be both published and open for pre-order, and a buyer cannot buy the same game twice.
    Borrowing folder 1's game would leave every request in those folders failing for a reason
    that has nothing to do with what they are demonstrating.

    Folder 1 is where each of these steps is actually explained.
    """
    items = [
        req(
            f"Create {title}",
            "POST",
            "{{catalogBase}}/v1/games",
            token="developerToken",
            idempotent=True,
            body={
                "title": title,
                "description": f"{title}, for this folder's own use.",
            },
            description="Its own game, so this folder runs independently of the others.",
            tests=capture(201, var),
        ),
        req(
            "Add its version",
            "POST",
            f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/versions",
            token="developerToken",
            idempotent=True,
            body={
                "version": "1.0.0",
                "file_ref": "{{buildId}}",
                "size_bytes": 1073741824,
            },
            description="A game needs one before it can be submitted — there is nothing to "
            "review otherwise.",
            tests=ok(201),
        ),
        req(
            "Submit it",
            "POST",
            f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/submit",
            token="developerToken",
            description="Into the review queue.",
            tests=ok(200),
        ),
        req(
            "Start its review",
            "POST",
            f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/review/start",
            token="supportToken",
            description="Claimed, so two reviewers do not work on it at once.",
            tests=ok(200),
        ),
        req(
            "Approve it",
            "POST",
            f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/review/approve",
            token="supportToken",
            body={"note": "Approved."},
            description="Approved is not published — the developer still decides when.",
            tests=ok(200),
        ),
        req(
            "Price it",
            "POST",
            f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/price",
            token="developerToken",
            idempotent=True,
            body={"amount_minor": price_minor, "currency": "IRR"},
            description=f"{price_minor // 100:,} IRR, in integer minor units.",
            tests=ok(200),
        ),
    ]
    if publish:
        items.append(
            req(
                "Publish it",
                "POST",
                f"{{{{catalogBase}}}}/v1/games/{{{{{var}}}}}/publish",
                token="developerToken",
                description="On sale.",
                tests=ok(200),
            )
        )
    return items


# =========================================================================
# 0. Setup
# =========================================================================

setup = folder(
    "0. Setup",
    "Run **Mint tokens** once, then work down the folders in order. Everything after it "
    "depends on the ids these requests capture.",
    [
        {
            "name": "Mint tokens",
            "event": [
                {
                    "listen": "prerequest",
                    "script": {"type": "text/javascript", "exec": MINT_SCRIPT},
                },
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "const names = ['userToken', 'developerToken', 'supportToken',",
                            "               'adminToken', 'platformToken'];",
                            "pm.test('every role token was minted', function () {",
                            "    names.forEach(function (name) {",
                            "        const value = pm.collectionVariables.get(name);",
                            "        pm.expect(value, name).to.be.a('string').and.not.empty;",
                            "    });",
                            "});",
                        ],
                    },
                },
            ],
            "request": {
                "method": "GET",
                "header": [],
                "url": url("{{catalogBase}}/livez"),
                "auth": {"type": "noauth"},
                "description": (
                    "Signs four HS256 tokens with the same `JWT_SECRET` the services verify "
                    "with, so this collection works with no Auth service running.\n\n"
                    "The request itself is only a health check — the work happens in the "
                    "pre-request script, and pointing it at `/healthz` means running this "
                    "folder also tells you whether the platform is up.\n\n"
                    "Four roles because the catalog's rules genuinely differ by role: a "
                    "**developer** submits a game, **support** reviews it, an **admin** runs "
                    "the sweeps, and a **basic user** buys it. Signing one token and reusing "
                    "it would hide every authorisation rule in the platform."
                ),
            },
        },
        req(
            "Provision the developer's wallet",
            "GET",
            "{{walletBase}}/v1/wallets/me",
            token="developerToken",
            description=(
                "A wallet is created on first access, so reading it is all it takes.\n\n"
                "Needed **before** anything is bought. The revenue split credits the developer, "
                "and a credit to a user with no wallet is dead-lettered — the purchase then "
                "sits at the split step with no visible error, which is exactly how it failed "
                "the first time the end-to-end suite ran this flow."
            ),
            tests=ok(200),
        ),
        req(
            "Provision the buyer's wallet",
            "GET",
            "{{walletBase}}/v1/wallets/me",
            description=(
                "The same call as the developer's, with the buyer's token. Without it the next "
                "request is a 404: there is no wallet to adjust."
            ),
            tests=ok(200),
        ),
        req(
            "Provision the platform's wallet",
            "GET",
            "{{walletBase}}/v1/wallets/me",
            token="platformToken",
            description=(
                "The platform is a wallet holder like anyone else — its 30% has to land "
                "somewhere. `platformUserId` must match `PLATFORM_USER_ID` in "
                "infra/deploy/compose/.env, or the split is credited to a wallet nobody reads."
            ),
            tests=ok(200),
        ),
        req(
            "Fund the buyer",
            "POST",
            "{{walletBase}}/v1/admin/wallets/{{userId}}/adjust",
            token="adminToken",
            idempotent=True,
            body={
                "direction": "CREDIT",
                "amount": {"amount_minor": "50000000", "currency": "IRR"},
                "reason": "seed balance for exercising the store collection",
            },
            description=(
                "500,000 IRR, enough for everything in this collection.\n\n"
                "Through the admin adjustment endpoint rather than a database write, so the "
                "ledger entry exists and the wallet's own reconciliation stays meaningful."
            ),
            tests=ok(200),
        ),
    ],
)

# =========================================================================
# 1. Publishing a game
# =========================================================================

publishing = folder(
    "1. Publishing a game",
    "Requirement 1.3, in the order it actually happens. A game cannot skip a step: the state "
    "machine refuses, and the reason code tells you which rule you hit.",
    [
        req(
            "Create a draft",
            "POST",
            "{{catalogBase}}/v1/games",
            token="developerToken",
            idempotent=True,
            body={
                "title": "Neon Drift",
                "description": "A synthwave racer through a city that never stops raining.",
                "genres": ["RACING", "ACTION"],
                "tags": ["synthwave", "singleplayer"],
            },
            description=(
                "A draft is private and has no price. Only the developer who created it can "
                "see it, which is why every other request in this folder uses the developer "
                "token."
            ),
            tests=capture(201, "gameId"),
        ),
        req(
            "Update the draft",
            "PATCH",
            "{{catalogBase}}/v1/games/{{gameId}}",
            token="developerToken",
            body={
                "description": "A synthwave racer. Now with a rival AI that learns your line."
            },
            description=(
                "Editable while it is a draft. Once submitted for review it is not: a "
                "reviewer must be looking at the same thing the developer submitted."
            ),
            tests=ok(200),
        ),
        req(
            "Add a version",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/versions",
            token="developerToken",
            idempotent=True,
            body={
                "version": "1.0.0",
                # A reference to the build in the media service — the catalog stores the id and
                # nothing about the bytes. It starts as a placeholder so this folder runs on its
                # own; the Media folder replaces it with a real upload's id.
                "file_ref": "{{buildId}}",
                "size_bytes": 4294967296,
                "notes": "First release.",
            },
            description=(
                "A game needs at least one version before it can be submitted — there is "
                "nothing to review otherwise."
            ),
            # `versions` is ordered oldest first, so the one just added is the last.
            tests=capture(201, "versionId", "versions[body.versions.length - 1].id"),
        ),
        req(
            "Submit for review",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/submit",
            token="developerToken",
            description=(
                "Hands the game to the review queue. The developer can no longer edit it.\n\n"
                "Try this on a game with no version to see `GAME_HAS_NO_VERSION` rather than a "
                "generic refusal."
            ),
            tests=ok(200),
        ),
        req(
            "See the review queue",
            "GET",
            "{{catalogBase}}/v1/review-queue",
            token="supportToken",
            description=(
                "Staff only. A developer asking for this gets a 403 — the queue is other "
                "developers' unreleased work."
            ),
            tests=ok(200),
        ),
        req(
            "Start the review",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/review/start",
            token="supportToken",
            description=(
                "Claims the game so two reviewers do not work on it at once. Records who is "
                "reviewing, which is the audit trail requirement 1.3 asks for."
            ),
            tests=ok(200),
        ),
        req(
            "Approve it",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/review/approve",
            token="supportToken",
            body={"note": "Content policy check passed."},
            description=(
                "Approved is not published. The developer still has to set a price and press "
                "publish — approval is permission, not a release."
            ),
            tests=ok(200),
        ),
        req(
            "Support suggests a price",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/suggest-price",
            token="supportToken",
            idempotent=True,
            body={"amount_minor": 900000, "currency": "IRR"},
            description=(
                "Requirement 1.3's pricing conversation, and it runs the direction that is easy "
                "to get backwards: **staff suggest, the developer decides.**\n\n"
                "Advisory only — it does not set anything. A reviewer who could price somebody "
                "else's game would be taking a business decision on their behalf. Sent with the "
                "support token; a developer calling this on their own game gets a 403."
            ),
            tests=ok(200),
        ),
        req(
            "Set the final price",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/price",
            token="developerToken",
            idempotent=True,
            body={"amount_minor": 1000000, "currency": "IRR"},
            description=(
                "10,000 IRR. Integer minor units and a string, like every amount on the "
                "platform.\n\n"
                "Only possible once the game is approved, and only by its developer."
            ),
            tests=ok(200),
        ),
        req(
            "Publish",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/publish",
            token="developerToken",
            description=(
                "Now it is for sale. Publishing emits `GamePublished`, which is what the "
                "recommendation and notification services listen for."
            ),
            tests=ok(200),
        ),
        anon(
            "Browse the store",
            "GET",
            "{{catalogBase}}/v1/games?limit=20&offset=0",
            description=(
                "Public and unauthenticated — a storefront that requires a login to browse is "
                "not a storefront. Published games only; a draft or a game in review is never "
                "in here."
            ),
            tests=ok(200),
        ),
        anon(
            "One game",
            "GET",
            "{{catalogBase}}/v1/games/{{gameId}}",
            description=(
                "The game itself, and nothing else. What a list row or a search result renders "
                "from.\n\n"
                "Separate from `/detail` on purpose: that one joins versions, media and the "
                "live promotion, and a browse page paying for all of it on every row would be "
                "the platform's slowest query for no benefit."
            ),
            tests=ok(200),
        ),
        req(
            "The game with its review history",
            "GET",
            "{{catalogBase}}/v1/games/{{gameId}}/detail",
            token="developerToken",
            description=(
                "The game plus its review trail — who reviewed it, when, and why they decided "
                "what they did.\n\n"
                "**Its developer and staff only, and authenticated**, which is what separates it "
                "from `GET /v1/games/{id}`: a rejection note is a conversation between a "
                "reviewer and a developer, and publishing it on the store page would expose "
                "both of them."
            ),
            tests=ok(200),
        ),
        req(
            "My games",
            "GET",
            "{{catalogBase}}/v1/games/mine",
            token="developerToken",
            description=(
                "A developer's own catalogue, drafts and rejections included. The only place "
                "an unpublished game is visible."
            ),
            tests=ok(200),
        ),
        req(
            "Reject a submission",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/review/reject",
            token="supportToken",
            body={"note": "Placeholder art in the trailer."},
            description=(
                "Refused here, because this game is already published — run it against a "
                "second game in `IN_REVIEW` to see it work.\n\n"
                "A rejection carries a reason the developer can act on. A rejection with no "
                "explanation produces a resubmission of the same thing."
            ),
        ),
        req(
            "Appeal a rejection",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/appeal",
            token="developerToken",
            body={"note": "The placeholder art was replaced in version 1.0.1."},
            description=(
                "Requirement 1.3 gives the developer a route back. An appeal returns the game "
                "to the queue rather than approving it — a reviewer still decides."
            ),
        ),
    ],
)

# =========================================================================
# 2. Media
# =========================================================================

media = folder(
    "2. Media",
    "Requirement 1.8. Screenshots are public; a build is not, and the difference is enforced "
    "rather than assumed.",
    [
        req(
            "Upload a screenshot",
            "POST",
            "{{mediaBase}}/v1/media",
            token="developerToken",
            idempotent=True,
            form=[
                {"key": "file", "type": "file", "src": "sample-screenshot.png"},
                {"key": "kind", "value": "IMAGE", "type": "text"},
                {"key": "reference_id", "value": "{{gameId}}", "type": "text"},
            ],
            description=(
                "`sample-screenshot.png` sits next to this collection and is referenced by "
                "name, so the request works unattended — pick your own file in the Body tab to "
                "try something else.\n\n"
                "The content type is decided from the **first bytes of the file**, not from "
                "what the uploader called it. An HTML page announced as a PNG is refused "
                "before a single byte is written, which is what stops a stored-XSS payload "
                "being served from the platform's own origin.\n\n"
                "An image defaults to `PUBLIC`: a store page has to be able to show it."
            ),
            tests=capture(201, "mediaId"),
        ),
        req(
            "Upload a build",
            "POST",
            "{{mediaBase}}/v1/media",
            token="developerToken",
            idempotent=True,
            form=[
                {"key": "file", "type": "file", "src": "sample-build.zip"},
                {"key": "kind", "value": "GAME_BINARY", "type": "text"},
                {"key": "reference_id", "value": "{{gameId}}", "type": "text"},
            ],
            description=(
                "Uses `sample-build.zip` from the same folder.\n\n"
                "Same endpoint, different default: a binary is `PRIVATE`. An unreleased build "
                "reachable by URL is the one leak that cannot be undone.\n\n"
                "Streamed in chunks and size-checked **while writing**, so a client that lies "
                "about `Content-Length` does not get the whole file onto disk before being "
                "told no."
            ),
            tests=capture(201, "buildId"),
        ),
        req(
            "My storage quota",
            "GET",
            "{{mediaBase}}/v1/media/usage",
            token="developerToken",
            description=(
                "How much of their share this developer has used.\n\n"
                "Exposed because a quota nobody can see is one you discover by having an "
                "upload refused after the bytes have already gone over the wire. Deleted "
                "media does not count — they gave the space back."
            ),
            tests=ok(200),
        ),
        req(
            "My uploads",
            "GET",
            "{{mediaBase}}/v1/media?limit=20&offset=0",
            token="developerToken",
            description="The caller's own files, public and private alike.",
            tests=ok(200),
        ),
        anon(
            "A game's public media",
            "GET",
            "{{mediaBase}}/v1/media/by-reference/{{gameId}}",
            description=(
                "How a store page fetches screenshots. **Public objects only, whoever asks** — "
                "the build sitting behind them must not appear here even for the developer, "
                "because one accidental use of this endpoint on an authenticated page would "
                "leak it."
            ),
            tests=ok(200),
        ),
        req(
            "Describe one file",
            "GET",
            "{{mediaBase}}/v1/media/{{buildId}}",
            token="developerToken",
            description=(
                "A file the caller may not read is reported **not found**, not forbidden. "
                '"Forbidden" confirms the id is real, which is enough to tell somebody '
                "enumerating ids that they have found an unreleased build."
            ),
            tests=ok(200),
        ),
        req(
            "Get a download ticket",
            "POST",
            "{{mediaBase}}/v1/media/{{buildId}}/ticket",
            token="developerToken",
            description=(
                "A short-lived signed URL — the local equivalent of an S3 presigned URL.\n\n"
                "Authorisation happens **here, once**, rather than on every byte of a download "
                "that may take twenty minutes. The token is the proof that it happened, and it "
                "is signed with a secret separate from `JWT_SECRET`: a download token is not "
                "an identity token, and one leaking must not compromise the other."
            ),
            tests=[
                "pm.test('status is 200', () => pm.response.to.have.status(200));",
                "const body = pm.response.json();",
                "pm.collectionVariables.set('downloadUrl', body.url);",
                "console.log('Signed URL (expires shortly): ' + body.url);",
            ],
        ),
        anon(
            "Download the public screenshot",
            "GET",
            "{{mediaBase}}/v1/media/{{mediaId}}/content",
            description=(
                "No token at all. Public means public, and requiring one would break every "
                "`<img>` tag on the store."
            ),
            tests=ok(200),
        ),
        anon(
            "Download a private build without a ticket",
            "GET",
            "{{mediaBase}}/v1/media/{{buildId}}/content",
            description=(
                "Refused. This request exists to be refused: it is the check that the private "
                "default actually means something.\n\n"
                "Paste the `url` from **Get a download ticket** into a browser to see the "
                "signed version succeed."
            ),
            tests=[
                "pm.test('a private build is not readable anonymously', function () {",
                "    pm.expect(pm.response.code).to.be.oneOf([401, 403, 404]);",
                "});",
            ],
        ),
        req(
            "Attach media to the game",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/media",
            token="developerToken",
            body={"media_ref": "{{mediaId}}", "kind": "IMAGE"},
            description=(
                "The catalog records the reference; the media service holds the bytes. Neither "
                "stores the other's job.\n\n"
                "This is the Anti-Corruption Layer in practice: the catalog knows a media id "
                "and nothing about checksums, magic bytes or where a file lives on disk.\n\n"
                "The two services also keep their **own** vocabularies — this `kind` is the "
                "catalog's `IMAGE`/`TEASER`, not the media service's `IMAGE`/`GAME_BINARY`/"
                "`TRAILER`. They overlap without being the same enum, which is the whole point "
                "of translating at the boundary rather than sharing a type."
            ),
            tests=ok(201),
        ),
        req(
            "Delete a file",
            "DELETE",
            "{{mediaBase}}/v1/media/{{mediaId}}",
            token="developerToken",
            description=(
                'Soft delete: the row stays so a reference to it resolves to "deleted" '
                "rather than a 404 an operator cannot explain. The space is given back to the "
                "owner's quota immediately.\n\n"
                "Skip this if you want the store page to keep its screenshot."
            ),
        ),
    ],
)

# =========================================================================
# 3. Buying
# =========================================================================

buying = folder(
    "3. Buying a game",
    "Requirement 1.4 and §6.1. A purchase is a saga across three services, so it comes back "
    "`202 Accepted` and completes a moment later.",
    [
        req(
            "Quote it first",
            "GET",
            "{{orderBase}}/v1/quotes/{{gameId}}",
            description=(
                "What this would cost, **charging nothing**. What a basket page calls.\n\n"
                "Add `?discount_code=...` to preview a code. Previewing does not consume it — "
                "a buyer who opened the page and changed their mind has not lost their reward."
            ),
            tests=ok(200),
        ),
        req(
            "Buy it",
            "POST",
            "{{orderBase}}/v1/orders",
            idempotent=True,
            body={"game_id": "{{gameId}}"},
            description=(
                "`202 Accepted`, not `201`. The order exists; the money has not moved yet.\n\n"
                "Debit the buyer → grant the game → credit the developer 70% and the platform "
                "30%. Each step is idempotent and each has a compensation, so a failure after "
                "the debit refunds rather than leaving somebody charged for nothing.\n\n"
                "Send this twice with the **same** `Idempotency-Key` and you get the original "
                "order back, not a second purchase. Sending it twice with different keys is "
                "refused with `GAME_ALREADY_OWNED` — which is the world's state, not a replay."
            ),
            tests=capture(202, "orderId"),
        ),
        req(
            "Watch it complete",
            "GET",
            "{{orderBase}}/v1/orders/{{orderId}}",
            description=(
                "Re-runs itself until the order reaches a settled state, which is how a "
                "collection waits for a saga: there is no sleep in the Postman sandbox, so the "
                "test re-queues this request and gives up after twenty passes rather than "
                "looping forever.\n\n"
                "In Postman by hand you can just press Send again a second later.\n\n"
                "Staff see the saga's step as well, because a support agent with a customer on "
                "the line needs to know a `PENDING` order is stuck at ownership. A buyer does "
                "not, and it exposes how the platform is put together."
            ),
            tests=poll_order(
                "COMPLETED",
                "FAILED",
                then=[
                    "pm.test('the shares add up to what was charged', function () {",
                    "    const dev = Number(body.developer_share.amount_minor);",
                    "    const plat = Number(body.platform_share.amount_minor);",
                    "    pm.expect(dev + plat).to.equal(Number(body.total_charged.amount_minor));",
                    "});",
                    "pm.test('the sale completed', function () {",
                    "    pm.expect(body.state).to.equal('COMPLETED');",
                    "});",
                ],
            ),
        ),
        req(
            "My library",
            "GET",
            "{{catalogBase}}/v1/library",
            description="The game is here once the saga finishes, not before.",
            tests=ok(200),
        ),
        req(
            "My orders",
            "GET",
            "{{orderBase}}/v1/orders?limit=20&offset=0",
            description=(
                "Orders the caller paid for, **plus gifts sent to them** — a recipient is "
                "entitled to see the order that gave them the game, and who sent it."
            ),
            tests=ok(200),
        ),
        req(
            "Check saleability",
            "GET",
            "{{catalogBase}}/v1/games/{{gameId}}/saleability",
            token="supportToken",
            description=(
                "The internal question the order service asks before every purchase: is this "
                "for sale, what does it cost right now, does this user already own it.\n\n"
                "One call rather than three, because it is on the critical path of every "
                "purchase on the platform."
            ),
            tests=ok(200),
        ),
        req(
            "Refund it",
            "POST",
            "{{orderBase}}/v1/orders/{{orderId}}/refund",
            idempotent=True,
            description=(
                "Requirement 1.4's twelve-hour window. Returns the money, takes the game back, "
                "and claws back both revenue shares.\n\n"
                "The order says `REFUNDING` until the **buyer's** credit is confirmed. The two "
                "reversals proceed independently: a developer who has already spent their "
                "share cannot be debited, the wallet records that as a shortfall for an "
                "operator, and it must not stop a buyer being told their money is back when it "
                "is."
            ),
            tests=ok(200),
        ),
    ],
)

# =========================================================================
# 4. Gifts
# =========================================================================

gifts = folder(
    "4. Gifts",
    "Requirement 1.4. The same saga with a different recipient, plus a 2% fee if a message is "
    "attached.",
    [
        req(
            "Send a game as a gift",
            "POST",
            "{{orderBase}}/v1/gifts",
            idempotent=True,
            body={
                "game_id": "{{gameId}}",
                "recipient_id": "{{friendId}}",
                "message": "Happy birthday. Try the rain level with the sound up.",
            },
            description=(
                "The **recipient** gets the game; the **buyer** pays. The recipient does not "
                "have to own it already and cannot decline it — requirement 1.4 on both "
                "counts.\n\n"
                "The message costs 2%, capped at 500 words. The fee goes entirely to the "
                "platform: it pays for a platform feature, and a developer's income should not "
                "change because a buyer chose to attach a note.\n\n"
                "A gift to yourself is refused rather than quietly charged the 2% for a "
                "message nobody reads."
            ),
            tests=capture(202, "giftOrderId"),
        ),
        req(
            "The gift order",
            "GET",
            "{{orderBase}}/v1/orders/{{giftOrderId}}",
            description=(
                "`total_charged` is the price plus the message fee; `developer_share` is 70% of "
                "the **price**, not of the total.\n\n"
                "Waits for the saga, because the next request cannot demonstrate its rule until "
                "this one has: a refund of an order that has not completed is refused for being "
                "incomplete, which says nothing about gifts."
            ),
            tests=poll_order(
                "COMPLETED",
                "FAILED",
                then=[
                    "pm.test('the message fee went to the platform', function () {",
                    "    const price = Number(body.base_price.amount_minor);",
                    "    const total = Number(body.total_charged.amount_minor);",
                    "    const dev = Number(body.developer_share.amount_minor);",
                    "    pm.expect(total).to.be.above(price);",
                    "    pm.expect(dev).to.equal(Math.round(price * 0.7));",
                    "});",
                ],
            ),
        ),
        req(
            "Try to refund a gift",
            "POST",
            "{{orderBase}}/v1/orders/{{giftOrderId}}/refund",
            idempotent=True,
            description=(
                "Refused, with `GIFT_NOT_REFUNDABLE`.\n\n"
                "Not an arbitrary restriction: the game is already in somebody else's library, "
                "and taking it back would punish the recipient for the buyer's change of mind."
            ),
            tests=[
                "pm.test('a gift cannot be refunded', function () {",
                "    pm.expect(pm.response.code).to.be.oneOf([409, 422]);",
                "    pm.expect(pm.response.text()).to.include('GIFT_NOT_REFUNDABLE');",
                "});",
            ],
        ),
    ],
)

# =========================================================================
# 5. Pre-orders
# =========================================================================

preorders = folder(
    "5. Pre-orders",
    "Requirement 1.5. The money is **reserved**, not spent, until the game ships.\n\n"
    "This folder builds its own game, because a game cannot be both published and open for "
    "pre-order — the state machine refuses, and borrowing the one from folder 1 would leave "
    "every request here failing for a reason that has nothing to do with pre-orders.",
    approval_chain("preorderGameId", "Neon Drift II", 1_500_000, publish=False)
    + [
        req(
            "Open pre-orders",
            "POST",
            "{{catalogBase}}/v1/games/{{preorderGameId}}/preorders",
            token="developerToken",
            body={"release_at": "2027-01-01T00:00:00Z"},
            description=(
                "Purchasable, but not released.\n\n"
                'The order service refuses a mismatch in **both** directions: pressing "buy" '
                "on something unreleased must not hold funds for weeks unexpectedly, and "
                'pressing "pre-order" on a released game must not defer a payment the buyer '
                "expected to make now."
            ),
            tests=ok(200),
        ),
        req(
            "Place a pre-order",
            "POST",
            "{{orderBase}}/v1/preorders",
            idempotent=True,
            body={"game_id": "{{preorderGameId}}"},
            description=(
                "A **hold**, not a debit. The buyer cannot spend the same balance twice, and the "
                "platform is not sitting on cash for a game that may never arrive. The price and "
                "the 70/30 split are decided now; only the moment the money moves is "
                "deferred.\n\n"
                "`PENDING` becomes `RESERVED` once the wallet confirms the hold. Not available "
                "as a gift: a gift is announced when it lands, and this lands weeks later."
            ),
            tests=capture(202, "preorderId"),
        ),
        req(
            "Watch it reserve",
            "GET",
            "{{orderBase}}/v1/orders/{{preorderId}}",
            description=(
                "`RESERVED`, and `refund_deadline` is **null** — deliberately. A reserved "
                "pre-order advertising a refund deadline would be actively misleading, because "
                "the way out of one is a cancellation, not a refund."
            ),
            tests=poll_order(
                "RESERVED",
                "FAILED",
                then=[
                    "pm.test('the funds are reserved', function () {",
                    "    pm.expect(body.state).to.equal('RESERVED');",
                    "});",
                    "pm.test('a reservation has no refund deadline', function () {",
                    "    pm.expect(body.refund_deadline == null).to.be.true;",
                    "});",
                ],
            ),
        ),
        req(
            "Check the hold on the wallet",
            "GET",
            "{{walletBase}}/v1/wallets/me",
            description=(
                "`available` is lower than `balance` by the price of the pre-order. That gap is "
                "the hold: the money is still theirs and they cannot spend it."
            ),
            tests=ok(200),
        ),
        req(
            "Release the game",
            "POST",
            "{{catalogBase}}/v1/games/{{preorderGameId}}/release",
            token="developerToken",
            description=(
                "Ships it, and this is what turns **every** hold against the game into a real "
                "purchase: the catalog emits `GameReleased`, and the order service captures each "
                "hold, grants the game and pays out the split.\n\n"
                "One event, an unbounded number of orders — which is why that path is batched, "
                "and why the batch loop is bounded rather than trusted to terminate."
            ),
            tests=ok(200),
        ),
        req(
            "The pre-order is now a purchase",
            "GET",
            "{{orderBase}}/v1/orders/{{preorderId}}",
            description="`COMPLETED`, the money captured from the hold, the game in the library, "
            "and the twelve-hour refund window now running from here.",
            tests=poll_order(
                "COMPLETED",
                "FAILED",
                then=[
                    "pm.test('the hold was captured and the sale completed', function () {",
                    "    pm.expect(body.state).to.equal('COMPLETED');",
                    "});",
                ],
            ),
        ),
        req(
            "Cancel a pre-order",
            "POST",
            "{{orderBase}}/v1/orders/{{preorderId}}/cancel",
            description=(
                "Refused here, because this one has already shipped — which is the rule, not a "
                "problem with the request. Run it while an order is still `RESERVED` to see it "
                "work.\n\n"
                "The hold is **released**, not refunded: nothing was ever taken, so there is "
                "nothing to give back. Once the game has shipped the way out is the ordinary "
                "twelve-hour refund."
            ),
        ),
    ],
)

# =========================================================================
# 6. Instalments
# =========================================================================

instalments = folder(
    "6. Instalments",
    "Requirement 3.3. The game is handed over after the **first** payment and the rest is "
    "collected on a schedule.\n\n"
    "Its own game again, for the plainest possible reason: the buyer already owns folder 1's, and "
    "you cannot buy a game twice.",
    approval_chain("planGameId", "Neon Drift: Rain Cup", 1_200_000, publish=True)
    + [
        req(
            "Buy on a payment plan",
            "POST",
            "{{orderBase}}/v1/instalment-orders",
            idempotent=True,
            body={"game_id": "{{planGameId}}", "instalments": 4, "interval_days": 30},
            description=(
                "A quarter of the price now, the game immediately, three more payments over "
                "the next three months.\n\n"
                "Withholding the game until the last payment would be layaway, not instalment "
                "credit. The cost of handing it over early is default risk: miss a payment for "
                "longer than the grace period and **the entitlement is revoked and what was "
                "already paid is not returned**. That is the deal, and it is stated plainly "
                "because it is the part a buyer needs to know.\n\n"
                "The developer is paid 70% of each payment **as it lands**, not of the price up "
                "front — paying that early would mean the platform lending them money and "
                "carrying the buyer's default risk.\n\n"
                "Not available as a gift, on a pre-order, or with a discount code. Three plans "
                "per buyer at once."
            ),
            tests=capture(202, "planOrderId"),
        ),
        req(
            "The schedule",
            "GET",
            "{{orderBase}}/v1/orders/{{planOrderId}}/instalment-plan",
            description=(
                "Every payment, what it is worth, when it is due, and whether it landed.\n\n"
                "`defaults_at` is the field that matters most: when the game gets taken back "
                "if nothing changes. `paid` and `outstanding` always add up to `total`."
            ),
            tests=[
                "pm.test('status is 200', () => pm.response.to.have.status(200));",
                "const body = pm.response.json();",
                "pm.test('paid and outstanding add up to the total', function () {",
                "    const paid = Number(body.paid.amount_minor);",
                "    const left = Number(body.outstanding.amount_minor);",
                "    pm.expect(paid + left).to.equal(Number(body.total.amount_minor));",
                "});",
            ],
        ),
        req(
            "The order while it is being paid",
            "GET",
            "{{orderBase}}/v1/orders/{{planOrderId}}",
            description=(
                "`PAYING`, not `COMPLETED`. The buyer has the game; the platform has not been "
                "paid for it.\n\n"
                "Deliberately a distinct state: a revenue report that counted this as a "
                "completed sale would overstate income by the whole outstanding balance."
            ),
            tests=ok(200),
        ),
        req(
            "Everything I am paying off",
            "GET",
            "{{orderBase}}/v1/instalment-plans",
            description=(
                "Closed plans included. A buyer asking what they are paying for also wants "
                "what they finished paying for — and a defaulted plan is the one thing they "
                "most need to be able to find an explanation of."
            ),
            tests=ok(200),
        ),
        req(
            "Pay it off early",
            "POST",
            "{{orderBase}}/v1/orders/{{planOrderId}}/pay-off",
            description=(
                "Settles everything outstanding at once. Not asked for by the requirement, but "
                "a plan you cannot pay off early is a worse product than one you can, and it "
                "costs nothing: the amounts and splits were fixed when the plan was drawn up, "
                "so settling together moves the same money in the same proportions.\n\n"
                "A wallet that cannot cover the lot pays off what it can; the rest go back to "
                "due, which is the same handling as any other refused payment."
            ),
            tests=ok(200),
        ),
        req(
            "Collect what is due now",
            "POST",
            "{{orderBase}}/v1/admin/instalments/collect",
            token="adminToken",
            description=(
                "Runs the collection sweep immediately instead of waiting for the timer.\n\n"
                "Staff only, and it exists for the same reason the saga sweep's endpoint does: "
                "when something is wrong at two in the morning, waiting out an interval to "
                "find out whether a fix worked is not debugging."
            ),
            tests=ok(200),
        ),
    ],
)

# =========================================================================
# 7. Promotions and withdrawal
# =========================================================================

promotions = folder(
    "7. Promotions and withdrawal",
    "Requirement 1.9. A festival discount needs the developer's agreement, because they bear "
    "70% of it.",
    [
        req(
            "Propose a discount",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/promotions",
            token="supportToken",
            idempotent=True,
            body={
                "discount_bps": 2500,
                "starts_at": "2026-08-01T00:00:00Z",
                "ends_at": "2026-08-08T00:00:00Z",
                "note": "Summer festival",
            },
            description=(
                "25% off, in **basis points** — 2500, exactly, with no float anywhere near a "
                "price.\n\n"
                "Proposed, not applied. The developer has to approve it, because the 70/30 "
                "split applies to the reduced price and they carry 70% of the reduction. A "
                "platform that could discount a game unilaterally would be spending somebody "
                "else's money."
            ),
            # The promotion just proposed is the PENDING one; a game may carry decided
            # promotions from earlier runs, so it is found by state rather than by position.
            tests=capture(
                201,
                "promotionId",
                "promotions.find((p) => p.state === 'PENDING').id",
            ),
        ),
        req(
            "Developer approves it",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/promotions/{{promotionId}}/approve",
            token="developerToken",
            body={"note": "Happy to run this one."},
            description=(
                "Now it is live inside its window. The **discount** is rounded rather than the "
                "result, and where two promotions overlap the largest wins — decided rather "
                "than left to whichever row came back first."
            ),
            tests=ok(200),
        ),
        req(
            "Developer rejects it",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/promotions/{{promotionId}}/reject",
            token="developerToken",
            body={"note": "Not while the launch discount is running."},
            description="The other half of the same decision. A rejected promotion never "
            "applies.",
        ),
        req(
            "Cancel a promotion",
            "DELETE",
            "{{catalogBase}}/v1/games/{{gameId}}/promotions/{{promotionId}}",
            token="supportToken",
            description="Ends it early. A sale already made at the discounted price stays as it "
            "was — an order's money never changes after the fact.",
        ),
        req(
            "Withdraw the game",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/withdraw",
            token="developerToken",
            body={"reason": "A licensing problem with the soundtrack."},
            description=(
                "Off sale. **Everyone who already bought it keeps it** — and a buyer whose "
                "purchase was in flight when the game was withdrawn still gets it, because "
                "they paid before it happened.\n\n"
                "Distinct from a rejection: nothing is wrong with the submission, the "
                "developer has pulled it."
            ),
        ),
        req(
            "Put it back",
            "POST",
            "{{catalogBase}}/v1/games/{{gameId}}/relist",
            token="developerToken",
            description="Back on sale at the same price, with no second review — it was already "
            "approved.",
        ),
        req(
            "Remove a version",
            "DELETE",
            "{{catalogBase}}/v1/games/{{gameId}}/versions/{{versionId}}",
            token="developerToken",
            description="Refused if it is the only one: a published game with no version is "
            "something nobody can download.",
        ),
        req(
            "Detach media",
            "DELETE",
            "{{catalogBase}}/v1/games/{{gameId}}/media/{{mediaId}}",
            token="developerToken",
            description="Removes the reference. The bytes are the media service's to delete.",
        ),
    ],
)

# =========================================================================
# 8. Staff and support
# =========================================================================

staff = folder(
    "8. Staff and support",
    "What a support agent and an operator need. Every request here is staff-only, and a basic "
    "user gets a 403.",
    [
        req(
            "Somebody else's library",
            "GET",
            "{{catalogBase}}/v1/users/{{userId}}/library",
            token="supportToken",
            description='A support agent answering "where is my game". A basic user asking '
            "for another user's library is refused.",
            tests=ok(200),
        ),
        req(
            "Somebody else's orders",
            "GET",
            "{{orderBase}}/v1/users/{{userId}}/orders?limit=20&offset=0",
            token="supportToken",
            description="The other half of the same question, from the order side.",
            tests=ok(200),
        ),
        req(
            "Does this user own this game",
            "GET",
            "{{catalogBase}}/v1/games/{{gameId}}/entitlement",
            description=(
                "The single question a launcher asks. Its own endpoint because it is called far "
                "more often than anything else here and needs to stay cheap."
            ),
            tests=ok(200),
        ),
        req(
            "Order states at a glance",
            "GET",
            "{{orderBase}}/v1/admin/order-states",
            token="adminToken",
            description=(
                "A count per state. `PENDING` should be near zero; a number that keeps growing "
                "means sagas are not finishing."
            ),
            tests=ok(200),
        ),
        req(
            "Nudge stalled sagas",
            "POST",
            "{{orderBase}}/v1/admin/sagas/sweep",
            token="adminToken",
            description=(
                "Finds sagas that have stopped moving and re-issues the command they are "
                "waiting on. Every command is idempotent, so re-issuing is safe.\n\n"
                "This runs on a timer anyway. The endpoint exists so an operator does not have "
                "to wait out the interval to find out whether a fix worked."
            ),
            tests=ok(200),
        ),
        req(
            "A basic user tries the admin endpoint",
            "POST",
            "{{orderBase}}/v1/admin/sagas/sweep",
            description=(
                "Refused. Here to prove the role check exists rather than to be assumed — the "
                "same endpoint, the same body, a different token."
            ),
            tests=[
                "pm.test('a basic user is refused', function () {",
                "    pm.expect(pm.response.code).to.be.oneOf([401, 403]);",
                "});",
            ],
        ),
    ],
)

# =========================================================================

collection = {
    "info": {
        "_postman_id": "a1b2c3d4-0002-4000-8000-arcadiastore01",
        "name": "Arcadia — Store, Orders & Media",
        "description": (
            "Every endpoint of the catalog, order and media services, in the order you would "
            "actually exercise them: publish a game, upload its screenshots, buy it, gift it, "
            "pre-order it, put it on a payment plan, discount it, refund it.\n\n"
            "## Getting started\n\n"
            "1. Start the platform: `cd infra/deploy/compose && docker compose up -d`\n"
            "2. Select the **Arcadia Local** environment.\n"
            "3. Run **0. Setup** end to end. It mints the role tokens, provisions the "
            "three wallets money moves between, and funds the buyer.\n"
            "4. Work down the folders in order. Later folders use ids the earlier ones "
            "captured.\n\n"
            "The companion collection, **Arcadia — Wallet & Payments**, covers money: "
            "balances, top-ups, gift cards, discount codes and the ledger.\n\n"
            "## Things worth knowing\n\n"
            "**Amounts are integers in the currency's minor unit**, and they are *strings* — "
            '`{"amount_minor": "1000000", "currency": "IRR"}` is 10,000 IRR. A string '
            "because a JavaScript client silently truncates integers above 2^53, and a price is "
            "not a thing to be approximately right about.\n\n"
            "**Rates are basis points, never percentages or floats.** 70% is 7000. 25% off is "
            "2500.\n\n"
            "**Buying returns `202 Accepted`, not `201`.** A purchase is a saga across three "
            "services — debit, grant, split — so the order exists before the money has moved. "
            "Re-read the order a second later and it is `COMPLETED`. This is the honest answer: "
            "reporting `201 Created` would claim the sale finished while the wallet had not yet "
            "been asked.\n\n"
            "**Every money-moving request carries an `Idempotency-Key`.** The collection "
            "generates a fresh UUID per request. Send one twice with the same key and you get "
            "the first response back with no second charge.\n\n"
            "**Four tokens, not one.** The rules genuinely differ by role, and signing a single "
            "token would hide every authorisation rule in the platform. A few requests here "
            "exist specifically to be **refused** — a gift refund, an anonymous private "
            "download, a basic user on an admin endpoint — because a guard nobody has seen fire "
            "is a guard nobody knows works.\n\n"
            "**Some requests need a file or a second game.** The media uploads need you to pick "
            "a file in the Body tab; the pre-order folder needs `preorderGameId` set to a game "
            "with pre-orders open. Both say so in their own description."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "auth": {
        "type": "bearer",
        "bearer": [{"key": "token", "value": "{{userToken}}", "type": "string"}],
    },
    "event": [
        {
            "listen": "prerequest",
            "script": {
                "type": "text/javascript",
                "exec": [
                    "// A fresh idempotency key per request, so every request in the collection",
                    "// is safe to re-run. Pin `pinnedIdempotencyKey` to reuse one deliberately",
                    "// and watch the replay guard work.",
                    "if (!pm.collectionVariables.get('pinnedIdempotencyKey')) {",
                    "    pm.collectionVariables.set('idempotencyKey', require('uuid').v4());",
                    "}",
                ],
            },
        },
        {
            "listen": "test",
            "script": {
                "type": "text/javascript",
                "exec": [
                    "// Applies to every request. A 4xx is often the point here — several",
                    "// requests exist to be refused — but a 5xx never is, and it should fail",
                    "// the run rather than be quietly reported as a pass.",
                    "pm.test('no server error', function () {",
                    "    pm.expect(pm.response.code).to.be.below(500);",
                    "});",
                ],
            },
        },
    ],
    "variable": [
        {"key": "catalogBase", "value": "http://localhost:8082", "type": "string"},
        {"key": "orderBase", "value": "http://localhost:8083", "type": "string"},
        {"key": "mediaBase", "value": "http://localhost:8084", "type": "string"},
        {"key": "walletBase", "value": "http://localhost:8080", "type": "string"},
        {"key": "idempotencyKey", "value": "", "type": "string"},
        {
            "key": "pinnedIdempotencyKey",
            "value": "",
            "type": "string",
            "description": "Set this to any value to stop the per-request key rotating.",
        },
        {"key": "userToken", "value": "", "type": "string"},
        {"key": "developerToken", "value": "", "type": "string"},
        {"key": "supportToken", "value": "", "type": "string"},
        {"key": "adminToken", "value": "", "type": "string"},
        {"key": "platformToken", "value": "", "type": "string"},
        {
            "key": "userId",
            "value": "11111111-1111-4111-8111-111111111111",
            "type": "string",
            "description": "The buyer.",
        },
        {
            "key": "developerId",
            "value": "22222222-2222-4222-8222-222222222222",
            "type": "string",
        },
        {
            "key": "supportId",
            "value": "33333333-3333-4333-8333-333333333333",
            "type": "string",
        },
        {
            "key": "adminId",
            "value": "44444444-4444-4444-8444-444444444444",
            "type": "string",
        },
        {
            "key": "platformUserId",
            "value": "00000000-0000-4000-8000-000000000001",
            "type": "string",
            "description": "Must match PLATFORM_USER_ID in infra/deploy/compose/.env.",
        },
        {
            "key": "friendId",
            "value": "55555555-5555-4555-8555-555555555555",
            "type": "string",
            "description": "The gift recipient.",
        },
        {"key": "gameId", "value": "", "type": "string"},
        {
            "key": "preorderGameId",
            "value": "",
            "type": "string",
            "description": "A second game with pre-orders open. Set it by hand — publishing and "
            "opening pre-orders are mutually exclusive on one game.",
        },
        {"key": "versionId", "value": "", "type": "string"},
        {"key": "mediaId", "value": "", "type": "string"},
        {
            "key": "buildId",
            "value": "pending-upload",
            "type": "string",
            "description": "The media id of the game's build. Defaults to a placeholder so the "
            "publishing folder runs on its own; the Media folder overwrites it with a real one.",
        },
        {"key": "downloadUrl", "value": "", "type": "string"},
        {
            "key": "pollAttempts",
            "value": "0",
            "type": "string",
            "description": "Bookkeeping for the requests that wait on a saga.",
        },
        {"key": "promotionId", "value": "", "type": "string"},
        {"key": "orderId", "value": "", "type": "string"},
        {"key": "giftOrderId", "value": "", "type": "string"},
        {"key": "preorderId", "value": "", "type": "string"},
        {"key": "planGameId", "value": "", "type": "string"},
        {"key": "planOrderId", "value": "", "type": "string"},
    ],
    "item": [
        setup,
        publishing,
        media,
        buying,
        gifts,
        preorders,
        instalments,
        promotions,
        staff,
    ],
}

# The mint script signs three roles in the wallet collection; the store needs a developer too.
mint = collection["item"][0]["item"][0]["event"][0]["script"]["exec"]
anchor = "pm.collectionVariables.set('userToken', sign(pm.collectionVariables.get('userId'), 'BASIC_USER'));"
assert anchor in mint, "the wallet collection's mint script changed shape"
for extra in (
    (
        "pm.collectionVariables.set('developerToken', "
        "sign(pm.collectionVariables.get('developerId'), 'DEVELOPER'));"
    ),
    # The platform holds a wallet like any other user, and its 30% has to land in one.
    (
        "pm.collectionVariables.set('platformToken', "
        "sign(pm.collectionVariables.get('platformUserId'), 'BASIC_USER'));"
    ),
):
    mint.insert(mint.index(anchor) + 1, extra)

environment = {
    "id": "b2c3d4e5-0003-4000-8000-arcadiastore01",
    "name": "Arcadia Local",
    "values": [
        {
            "key": "jwtSecret",
            "value": "local-development-jwt-secret-change-me-please",
            "type": "secret",
            "enabled": True,
            "description": (
                "Must match JWT_SECRET in infra/deploy/compose/.env. The collection signs its "
                "own tokens with this, which is how it works with no Auth service running. "
                "Minimum 32 bytes."
            ),
        },
        {
            "key": "jwtIssuer",
            "value": "arcadia-auth",
            "type": "default",
            "enabled": True,
            "description": "Must match JWT_ISSUER, or every request is rejected as "
            "unauthenticated.",
        },
        {
            "key": "jwtAudience",
            "value": "arcadia",
            "type": "default",
            "enabled": True,
            "description": "Must match JWT_AUDIENCE.",
        },
    ],
    "_postman_variable_scope": "environment",
}

OUT_DIR.mkdir(parents=True, exist_ok=True)
(OUT_DIR / "arcadia-store.postman_collection.json").write_text(
    json.dumps(collection, indent=2, ensure_ascii=False) + "\n"
)
(OUT_DIR / "arcadia-local.postman_environment.json").write_text(
    json.dumps(environment, indent=2, ensure_ascii=False) + "\n"
)

count = sum(len(f["item"]) for f in collection["item"])
print(f"{len(collection['item'])} folders, {count} requests")
