# Migrations

The storage schema and the migrations that move it.

## Paths

- `runway/storage/migrations/**`
- `tests/test_migrations.py`

## How to verify

- `poetry run python -m unittest tests.test_migrations`: every migration from `0001` upgrades and downgrades on a scratch database.

## Not verifiable yet

- Nothing
