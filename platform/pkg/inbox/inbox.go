// Package inbox makes Kafka consumers idempotent.
//
// The outbox guarantees at-least-once delivery, so a consumer will occasionally
// see the same event twice: the broker redelivered after a rebalance, or the
// dispatcher published then crashed before recording success. Crediting a wallet
// twice for one bank payment is not an acceptable outcome.
//
// The inbox closes that gap. Before handling an event, the consumer inserts its
// event id into `processed_events` inside the same transaction as the state
// change. The primary key makes the second attempt fail, the handler sees
// "already processed", and the money moves exactly once.
package inbox

import (
	"context"
	"fmt"
	"time"

	"github.com/MS-Arcadia/arcadia-platform/pkg/postgres"
	"github.com/jackc/pgx/v5"
)

// Store records which events a service has already handled.
type Store struct {
	// consumer names the logical consumer, so that two independent handlers of the
	// same event (say the ledger projection and the notifier) do not shadow each
	// other's bookkeeping.
	consumer string
}

// NewStore returns a Store for the named consumer.
func NewStore(consumer string) *Store {
	if consumer == "" {
		consumer = "default"
	}
	return &Store{consumer: consumer}
}

// Record claims an event for processing within tx.
//
// It returns true when this is the first time the event has been seen and the
// caller should proceed, and false when the event was already handled and the
// caller must skip it. Either way the caller must commit tx: on a duplicate
// there is simply nothing else to do.
func (s *Store) Record(ctx context.Context, tx pgx.Tx, eventID, eventType string, at time.Time) (bool, error) {
	if eventID == "" {
		return false, fmt.Errorf("inbox: event id is required")
	}

	tag, err := tx.Exec(ctx,
		`INSERT INTO processed_events (event_id, consumer, event_type, processed_at)
		 VALUES ($1, $2, $3, $4)
		 ON CONFLICT (event_id, consumer) DO NOTHING`,
		eventID, s.consumer, eventType, at.UTC(),
	)
	if err != nil {
		return false, fmt.Errorf("inbox: record %s: %w", eventID, err)
	}
	// Zero rows affected means the ON CONFLICT clause fired: a duplicate.
	return tag.RowsAffected() == 1, nil
}

// Seen reports whether an event was already processed, without claiming it.
// Handy for diagnostics; the transactional Record is what enforces correctness.
func (s *Store) Seen(ctx context.Context, q postgres.Querier, eventID string) (bool, error) {
	var exists bool
	err := q.QueryRow(ctx,
		`SELECT EXISTS (SELECT 1 FROM processed_events WHERE event_id = $1 AND consumer = $2)`,
		eventID, s.consumer,
	).Scan(&exists)
	if err != nil {
		return false, fmt.Errorf("inbox: check %s: %w", eventID, err)
	}
	return exists, nil
}

// Purge deletes bookkeeping rows older than the retention window. The window
// only needs to outlive the broker's own retention plus any realistic
// redelivery delay.
func (s *Store) Purge(ctx context.Context, tx pgx.Tx, before time.Time) (int64, error) {
	tag, err := tx.Exec(ctx,
		`DELETE FROM processed_events WHERE consumer = $1 AND processed_at < $2`,
		s.consumer, before.UTC(),
	)
	if err != nil {
		return 0, fmt.Errorf("inbox: purge: %w", err)
	}
	return tag.RowsAffected(), nil
}
