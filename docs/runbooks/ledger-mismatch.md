# Runbook: `LedgerMismatch`

**Severity: page.** A wallet balance no longer equals the sum of its ledger entries.

This is the one alert in the platform that means the accounting itself is wrong. Everything
else — a stalled saga, a dead-lettered command, an unreachable bank — is recoverable
because the ledger still tells the truth. This one says it might not.

---

## What fired

`arcadia_ledger_mismatch_count > 0`, published by the wallet service's reconciliation job
(every 15 minutes by default). The same finding is on `wallet-events` as
`LedgerMismatchDetected`, one event per affected wallet, with the stored balance, the ledger
sum and the delta.

## The rule that broke

`wallets.balance_minor` is a **cached projection** of `ledger_entries`. Every balance change
appends exactly one entry, in the same transaction, and the sum of the signed amounts must
therefore always reproduce the balance. Reconciliation checks that with a lateral join.

## Do not do this

**Do not update the balance to match the ledger, or the ledger to match the balance.**

The balance is a cache; the ledger is history. Editing either destroys the evidence needed
to find out what happened. The ledger will not let you: `ledger_entries` has triggers that
reject `UPDATE`, `DELETE` and `TRUNCATE`. That is on purpose, and it is not to be disabled.

---

## Triage

### 1. Establish scope

```sql
-- Which wallets, and by how much.
SELECT w.id, w.user_id, w.balance_minor,
       coalesce(l.ledger_sum, 0) AS ledger_sum,
       w.balance_minor - coalesce(l.ledger_sum, 0) AS delta
FROM wallets w
LEFT JOIN LATERAL (
  SELECT sum(CASE WHEN direction = 'DEBIT' THEN -amount_minor ELSE amount_minor END) AS ledger_sum
  FROM ledger_entries WHERE wallet_id = w.id
) l ON true
WHERE w.balance_minor <> coalesce(l.ledger_sum, 0)
ORDER BY abs(w.balance_minor - coalesce(l.ledger_sum, 0)) DESC;
```

**One wallet** points at a specific transaction. **Many wallets, similar deltas** points at
a code path or a batch job. **Every wallet** points at a migration or a manual `UPDATE`.

### 2. Freeze the affected wallets

Stops further movement while you investigate, and stops a user spending money that may not
be theirs.

```bash
curl -X POST "$WALLET/v1/admin/wallets/$USER_ID/freeze" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"ledger mismatch investigation, incident INC-____"}'
```

A freeze blocks credits as well as debits, and the affected user will notice. Say so in the
incident channel.

### 3. Find the divergence

Read the wallet's history in order and watch where `balance_after` stops matching the
running total:

```sql
SELECT sequence, direction, amount_minor, balance_after_minor, reason,
       reference_id, idempotency_key, created_at
FROM ledger_entries
WHERE wallet_id = '<wallet-id>'
ORDER BY sequence;
```

`balance_after_minor` exists for exactly this moment: each entry records the balance
immediately after it was applied, so the first row where the chain breaks is the first
movement that went wrong.

### 4. Identify which failure it was

| What you see | Likely cause |
|---|---|
| An entry whose `balance_after` does not follow from the previous one | The wallet was updated without its entry, or with the wrong amount. A code bug — find the deploy. |
| A balance higher than the ledger, no suspicious entry | A credit applied without an entry. Check `outbox_messages` and application logs around that time. |
| A balance lower than the ledger | A debit applied without an entry. Same. |
| Two entries sharing an `idempotency_key` | The idempotency guard was bypassed. Check whether `ledger_wallet_idempotency_key_idx` still exists. |
| Every wallet off by the same amount | A migration or a manual `UPDATE`. Check `schema_migrations` and the Postgres logs. |
| Nothing in the ledger looks wrong at all | Somebody ran `UPDATE wallets`. Check `pg_stat_activity` history and audit access. |

Cross-check against the audit stream, which is a second, independent record:

```bash
kafka-console-consumer.sh --bootstrap-server "$BROKER" \
  --topic audit-events --from-beginning \
  | grep '<wallet-id>'
```

If the audit trail agrees with the ledger and disagrees with the balance, the ledger is
sound and only the projection is wrong — which is the better outcome.

### 5. Correct it

Once you know what happened, fix it **by appending**, never by editing:

```bash
curl -X POST "$WALLET/v1/admin/wallets/$USER_ID/adjust" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d '{
        "direction": "CREDIT",
        "amount": {"amount_minor": "50000", "currency": "IRR"},
        "reason": "correcting ledger mismatch, incident INC-____ — see runbook"
      }'
```

The adjustment writes an `ADJUSTMENT` entry attributed to the operator who made it, which
is what keeps the history auditable afterwards. The justification is mandatory; write the
incident id in it.

Then re-run reconciliation and unfreeze:

```bash
curl -X POST "$WALLET/v1/admin/reconcile?user_id=$USER_ID" -H "Authorization: Bearer $ADMIN_TOKEN"
curl -X POST "$WALLET/v1/admin/wallets/$USER_ID/unfreeze" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"reason":"corrected under INC-____"}'
```

---

## If the cause is a code bug

The mismatch is a symptom. The bug will keep producing them.

1. Roll back the deploy that introduced it, if you can identify it.
2. Confirm the guard that should have caught it is intact:
   * `CHECK (balance_minor >= 0)` on `wallets`
   * the append-only triggers on `ledger_entries`
   * `ledger_wallet_idempotency_key_idx`
   * the `version` column and the `WHERE version = $n` clause in `WalletRepo.Update`
3. Add the case to `internal/app` as a failing test **before** fixing it. Every existing
   money-path test was written the same way, and this is the class of bug that regresses.

## Afterwards

The interesting question is not "which balance was wrong" but "how did a movement reach the
database without its entry". Every write goes through one code path
(`core.recordMovement`) precisely so that this cannot happen; a mismatch means something
bypassed it. Find out what, and close the route.
