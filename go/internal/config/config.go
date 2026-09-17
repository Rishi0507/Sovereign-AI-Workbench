// Package config loads JSON configuration files strictly: unknown fields are errors.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
)

// Validator is implemented by configuration structs that check their own values.
type Validator interface {
	Validate() error
}

// Load decodes path into dst, rejecting unknown fields and trailing data, then validates.
func Load(path string, dst any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read config %s: %w", path, err)
	}
	return Decode(data, dst)
}

// Decode is Load for in-memory data.
func Decode(data []byte, dst any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("decode config: %w", err)
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("decode config: trailing data after the JSON object")
	}
	if v, ok := dst.(Validator); ok {
		if err := v.Validate(); err != nil {
			return fmt.Errorf("invalid config: %w", err)
		}
	}
	return nil
}
