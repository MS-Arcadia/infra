# Postman — store, orders and media

`arcadia-store.postman_collection.json` is every endpoint of the **catalog**, **order** and
**media** services — 82 requests — in the order you would actually exercise them: publish a game,
upload its screenshots, buy it, gift it, pre-order it, put it on a payment plan, discount it,
withdraw it, refund it.

Its companion, [`wallet-service/api/postman`](../../../wallet-service/api/postman), covers money:
balances, top-ups, gift cards, discount codes and the ledger. Between them they cover the whole
platform.

## Running it

```bash
docker compose -f infra/deploy/compose/docker-compose.yml --env-file infra/deploy/compose/.env up -d
```

In Postman: import both files, select the **Arcadia Local** environment, run **0. Setup**, then
work down the folders.

Unattended:

```bash
npx newman run arcadia-store.postman_collection.json -e arcadia-local.postman_environment.json --delay-request 500
```

**`--delay-request` is not optional.** Several requests wait on a saga by re-queueing themselves
until the order settles, and the Postman sandbox has no `sleep` — without a delay the retries fire
back to back and the whole allowance is spent in under a second. 500ms gives each wait about
twenty seconds, which covers a release: one `GameReleased` turns every hold against the game into
a purchase, across three services and several Kafka round trips.

Last verified run: **98 requests, 204 assertions, 0 failures, 53s.**

## What it assumes

**Everything comes from `0. Setup`.** It signs five HS256 tokens with the same `JWT_SECRET` the
services verify with — buyer, developer, support, admin, and the platform's own wallet — so the
collection works with no Auth service running. It then provisions the three wallets money moves
between and funds the buyer.

The wallets matter more than they look. The revenue split credits the developer, and a credit to a
user with no wallet is dead-lettered: the purchase then sits at the split step with no visible
error anywhere. That is exactly how this flow failed the first time the end-to-end suite ran it,
and it is why provisioning is three explicit requests rather than a footnote.

**Folders 5 and 6 build their own games.** A game cannot be both published and open for
pre-order, and a buyer cannot buy the same game twice — so borrowing folder 1's game would leave
every request in those folders failing for a reason that has nothing to do with pre-orders or
instalments. Folder 1 is where each step of the approval path is explained; the copies in 5 and 6
are deliberately terse.

**The two media uploads use real files** — `sample-screenshot.png` and `sample-build.zip` sit next
to the collection and are referenced by name, so the collection runs unattended. Pick your own
file in the Body tab to try something else; a PNG announced as a ZIP is refused, which is worth
seeing.

## Some requests are meant to fail

A guard nobody has watched fire is a guard nobody knows works. These are in the collection because
of what they refuse:

| Request | Refused with | Why the rule exists |
|---|---|---|
| `4. Gifts` → Try to refund a gift | `GIFT_NOT_REFUNDABLE` | The game is already in somebody else's library. Taking it back punishes the recipient for the buyer's change of mind. |
| `2. Media` → Download a private build without a ticket | 401/403/404 | An unreleased build reachable by URL is the one leak that cannot be undone. |
| `8. Staff` → A basic user tries the admin endpoint | 401/403 | Same endpoint, same body, different token. |
| `1. Publishing` → Reject a submission / Appeal | wrong state | Both need a game in review; this one is published. Run them against a second game. |
| `5. Pre-orders` → Cancel a pre-order | wrong state | Once the game has shipped the way out is the ordinary refund, not a cancellation. |

The collection-level test asserts only that nothing returns a **5xx**. A 4xx is often the point;
a 500 never is.

## Editing it

The JSON is generated. Edit [`generate.py`](generate.py) and re-run it:

```bash
python3 infra/api/postman/generate.py
```

82 requests of deeply nested JSON is not a thing to hand-edit — that is how a URL ends up
disagreeing with its own `path` array, which Postman resolves in favour of the array and nobody
notices. The generator derives `path` from the URL string so the two cannot diverge.

## Things worth knowing about the API

**Amounts are integers in the currency's minor unit, and they are strings.**
`{"amount_minor": "1000000", "currency": "IRR"}` is 10,000 IRR. A string because a JavaScript
client silently truncates integers above 2^53, and a price is not a thing to be approximately
right about.

**Rates are basis points, never percentages or floats.** 70% is `7000`. 25% off is `2500`.

**Buying returns `202 Accepted`, not `201`.** A purchase is a saga across three services — debit,
grant, split — so the order exists before the money has moved. Re-read it a second later and it is
`COMPLETED`. Returning `201 Created` would claim the sale finished while the wallet had not yet
been asked.

**`PAYING` is not `COMPLETED`.** An instalment sale is delivered and still being collected. A
revenue report that counted it as complete would overstate income by the whole outstanding
balance.

**Every money-moving request carries an `Idempotency-Key`**, generated fresh per request. Set
`pinnedIdempotencyKey` to any value to stop it rotating, then send something twice and watch the
replay guard work.

## What running this found

Both collections had never been executed end to end. Doing it found real problems, which is the
argument for keeping them runnable rather than treating them as documentation:

- **The token script never ran at all.** `const CryptoJS = require('crypto-js')` redeclares a
  global the Postman sandbox already provides, so the script died with "Identifier already
  declared" and *every* request in the wallet collection was a 401. Fixed in the wallet collection
  too, since the store collection borrows its signing script verbatim.
- **`suggest-price` was documented backwards.** It is staff proposing a price *to* the developer,
  not the developer asking for a suggestion — and it is staff-only, so the version in this
  collection was a guaranteed 403.
- **`/games/{id}/detail` is not a public store page.** It carries the review history, and that is
  a conversation between a reviewer and a developer.
- **Two ids were captured wrong.** Adding a version and proposing a promotion both answer with the
  whole game, so `body.id` is the *game's* id — `versionId` and `promotionId` each held a game id
  and every request using them 404'd.
- **Waiting on a saga asserted on every retry**, so a wait that succeeded on the fourteenth
  attempt still ended the run with thirteen red assertions and nothing to say they were expected.
  Indistinguishable from a genuinely broken release.
- **Two live bugs in the Go services**, still open: the payment service returns a 500 with a raw
  Postgres error for a non-UUID id in the URL, and a bank top-up fails with a misleading 401 —
  the caller authenticated fine; it is the wallet's own call to the payment service that is
  refused, and a 401 tells a client to re-login when that cannot possibly help.
