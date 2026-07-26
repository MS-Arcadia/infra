package redisx

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// ErrLockNotAcquired is returned when the lock is held by somebody else.
var ErrLockNotAcquired = errors.New("redisx: lock not acquired")

// unlockScript releases a lock only if this holder still owns it.
//
// Comparing the token before deleting is essential. Without it, a holder whose
// lease expired mid-operation would delete a lock that a second holder has since
// acquired, and two workers would run the critical section at once.
const unlockScript = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
`

// extendScript renews a lease, again only for the current owner.
const extendScript = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
`

// Locker mints distributed locks.
//
// This is a single-instance lock (SET NX PX), not Redlock. That is the honest
// trade-off for this platform: it is correct while the Redis primary is up, and
// every critical section it guards is also protected by a database-level
// constraint or row lock, so a lock lost to a Redis failover degrades throughput
// rather than correctness.
type Locker struct {
	client *Client
	unlock *redis.Script
	extend *redis.Script
	prefix string
}

// NewLocker returns a Locker whose keys are namespaced by prefix.
func NewLocker(client *Client, prefix string) *Locker {
	return &Locker{
		client: client,
		unlock: redis.NewScript(unlockScript),
		extend: redis.NewScript(extendScript),
		prefix: prefix,
	}
}

// Lock is an acquired lease.
type Lock struct {
	locker *Locker
	key    string
	token  string
	ttl    time.Duration
}

// Acquire tries once to take the lock.
func (l *Locker) Acquire(ctx context.Context, name string, ttl time.Duration) (*Lock, error) {
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	key := l.key(name)
	token := uuid.NewString()

	acquired, err := l.client.Raw().SetNX(ctx, key, token, ttl).Result()
	if err != nil {
		return nil, fmt.Errorf("redisx: acquire lock %s: %w", name, err)
	}
	if !acquired {
		return nil, ErrLockNotAcquired
	}
	return &Lock{locker: l, key: key, token: token, ttl: ttl}, nil
}

// AcquireWait retries with jittered backoff until the lock is taken or the
// deadline passes. The jitter matters: without it, a fleet of workers woken by the
// same scheduler tick would retry in lockstep forever.
func (l *Locker) AcquireWait(ctx context.Context, name string, ttl, maxWait time.Duration) (*Lock, error) {
	deadline := time.Now().Add(maxWait)
	backoff := 20 * time.Millisecond

	for {
		lock, err := l.Acquire(ctx, name, ttl)
		if err == nil {
			return lock, nil
		}
		if !errors.Is(err, ErrLockNotAcquired) {
			return nil, err
		}
		if time.Now().After(deadline) {
			return nil, ErrLockNotAcquired
		}

		jitter := time.Duration(rand.Int64N(int64(backoff)))
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff + jitter):
		}
		if backoff < time.Second {
			backoff *= 2
		}
	}
}

// WithLock runs fn while holding the named lock, releasing it afterwards.
func (l *Locker) WithLock(ctx context.Context, name string, ttl time.Duration, fn func(ctx context.Context) error) error {
	lock, err := l.Acquire(ctx, name, ttl)
	if err != nil {
		return err
	}
	defer func() {
		// Release with a detached context so that a canceled request still gives the
		// lock back instead of leaving it to expire.
		releaseCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = lock.Release(releaseCtx)
	}()
	return fn(ctx)
}

// Release gives the lock back if this holder still owns it.
func (lock *Lock) Release(ctx context.Context) error {
	released, err := lock.locker.unlock.Run(ctx, lock.locker.client.Raw(),
		[]string{lock.key}, lock.token).Int64()
	if err != nil {
		return fmt.Errorf("redisx: release lock %s: %w", lock.key, err)
	}
	if released == 0 {
		// The lease expired and possibly changed hands. Worth knowing about: it means
		// the critical section outlived its TTL.
		return fmt.Errorf("redisx: lock %s was no longer held at release time", lock.key)
	}
	return nil
}

// Extend renews the lease, for a critical section that is taking longer than
// expected.
func (lock *Lock) Extend(ctx context.Context, ttl time.Duration) error {
	extended, err := lock.locker.extend.Run(ctx, lock.locker.client.Raw(),
		[]string{lock.key}, lock.token, ttl.Milliseconds()).Int64()
	if err != nil {
		return fmt.Errorf("redisx: extend lock %s: %w", lock.key, err)
	}
	if extended == 0 {
		return ErrLockNotAcquired
	}
	lock.ttl = ttl
	return nil
}

func (l *Locker) key(name string) string {
	return fmt.Sprintf("lock:%s:%s", l.prefix, name)
}

// fastRand returns a random int64 for building unique member ids.
func fastRand() int64 { return rand.Int64() }
