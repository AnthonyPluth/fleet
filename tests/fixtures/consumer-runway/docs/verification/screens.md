# Screens and flows

The web app's pages and the flows the verify driver walks through.

## Paths

- `frontend/verify/**`
- `runway/static/**`
- `screenshots/**`

## How to verify

- `node frontend/verify/verify.mjs`: drives every flow in `frontend/verify/flows/` against the app with the fake provider, at phone and desktop widths, and writes the screenshots.
- `tests/test_budget.py`: the budget totals the budget page shows.

## Not verifiable yet

- A live bank connection's first sync: the provider has no fake, so a flow can only use canned data; a fake under `tests/fakes/` would make it testable.
