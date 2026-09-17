// Package ringbuf keeps the last N bytes written to it.
package ringbuf

import "sync"

// Buffer is an io.Writer that retains only the most recent Limit bytes.
type Buffer struct {
	mu    sync.Mutex
	limit int
	data  []byte
	total int64
}

// New returns a buffer that keeps at most limit bytes.
func New(limit int) *Buffer {
	if limit < 1 {
		limit = 1
	}
	return &Buffer{limit: limit}
}

// Write appends p and discards the oldest bytes beyond the limit.
func (b *Buffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.total += int64(len(p))
	if len(p) >= b.limit {
		b.data = append(b.data[:0], p[len(p)-b.limit:]...)
		return len(p), nil
	}
	b.data = append(b.data, p...)
	if over := len(b.data) - b.limit; over > 0 {
		b.data = append(b.data[:0], b.data[over:]...)
	}
	return len(p), nil
}

// String returns the retained bytes.
func (b *Buffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.data)
}

// Total reports how many bytes were written in total.
func (b *Buffer) Total() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.total
}

// Truncated reports whether bytes were discarded.
func (b *Buffer) Truncated() bool { return b.Total() > int64(len(b.String())) }
