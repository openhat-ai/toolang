# Fingerprint Rules

This note defines fingerprint calculation rules for prepared state in the
runtime.


## Goals

- Keep change detection deterministic.
- Separate source change detection from prepared output identity.
- Make directory-backed definitions behave predictably.


## Source Fingerprint

Each entry records one source fingerprint at:

```json
"source": {
  "path": "...",
  "updated_at": "...",
  "fingerprint": "..."
}
```

`source.fingerprint` answers:

- has the watched source changed?

It is calculated from `source.path`.


## File Source Rule

When `source.path` is a file:

- read the file bytes
- compute `sha256(file_bytes)`
- encode the result as lowercase hex

`source.updated_at` is the file's last update time, serialized as a UTC
ISO8601 string.


## Directory Source Rule

When `source.path` is a directory:

- recursively collect all files
- sort files by relative path
- compute `sha256(file_bytes)` for each file
- build a stable byte stream from:
  - `relative_path`
  - `"\0"`
  - `file_sha256`
  - `"\n"`
- compute `sha256(...)` over the full stream
- encode the result as lowercase hex

`source.updated_at` is the maximum update time across all files in that
directory tree, serialized as a UTC ISO8601 string.


## Lock Fingerprint

`lock.json.fingerprint` answers:

- does this prepared output differ from the previous prepared output?

It should be derived from prepared runtime content, not from timestamps.

The lock fingerprint should be computed from stable per-entry contributions,
sorted deterministically, then hashed with `sha256`.


## Entry Contribution

Each entry should contribute a normalized object containing:

- `kind`
- `name`
- `shape`
- `locator`
- `path`
- `source.form`
- `source.path`
- `source.fingerprint`
- `meta`
- `content_fingerprint`

`content_fingerprint` represents runtime-visible prepared content:

- when `shape == "file"`
  - hash the bytes of `path`
- when `shape == "dir"`
  - hash the full directory rooted at `path.parent` using the directory rule


## Stable Ordering

Entries should be ordered by:

1. `kind`
2. `name`
3. `locator`

The normalized entry list should then be serialized with stable JSON key
ordering before hashing.


## Excluded Fields

The following fields should not affect the lock fingerprint:

- `updated_at`
- any temporary path outside the prepared output shape

Timestamps are for observation only. Fingerprints define identity.
