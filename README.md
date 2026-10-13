# Itinerary Desk MCP (Duffel flights) — TEST MODE ONLY

Dependency-free, stateless MCP server (protocol 2026-07-28; also 2025-11-25/06-18/03-26). stdio by default, Streamable HTTP with `--http` (POST `/mcp`, `GET /healthz`).

## Tools
| Tool | Duffel v2 call | Safety |
|---|---|---|
| `search_flights` | `POST /air/offer_requests?return_offers=true` | read-only |
| `get_offer` | `GET /air/offers/{id}` | read-only |
| `price_check` | `GET /air/offers/{id}` (live reprice, hold eligibility via `payment_requirements.requires_instant_payment`) → issues confirm tokens | read-only |
| `book_order` | `POST /air/orders` (`type: instant` + `payments:[{type: balance}]`, or `type: hold`) | destructiveHint; confirm=true + confirm_token + idempotency_key |
| `get_order` | `GET /air/orders/{id}` | read-only |
| `cancel_order` | step 1 `POST /air/order_cancellations` (pending quote) → step 2 `POST /air/order_cancellations/{id}/actions/confirm` | destructive at step 2 |
| `change_order` | step 1 `POST /air/order_change_requests` (options) → step 2 `POST /air/order_changes` + `POST /air/order_changes/{id}/actions/confirm` (balance payment if delta > 0) | destructive at step 2 |

Headers: `Authorization: Bearer`, `Duffel-Version: v2`.

## Safety model
- **Test mode only.** If `DUFFEL_ACCESS_TOKEN` is set and does not start with `duffel_test_`, every tool refuses.
- **No token → fixtures.** `fixtures.py` simulates Duffel Airways (ZZ) with Duffel-shaped responses so tests and demos work offline. Every response carries `mode`.
- **Confirm tokens.** Quote tools return an HMAC token bound to the exact action, ids, amount and order type, valid 10 min. Writes need that token, `confirm: true`, and an `idempotency_key`. Tampered amounts fail.
- **Idempotency.** Replay store persisted to a JSON file (`ITINERARY_IDEM_PATH`, default `.idempotency.json`) so repeat keys survive restarts; key also stored in Duffel order `metadata`. Single-instance only (no shared DB; Render free disk is ephemeral across deploys).
- Set `ITINERARY_CONFIRM_SECRET` so tokens survive restarts (render.yaml generates one).

## Access key (recommended for any public deploy)
Set `ITINERARY_API_KEY` to a long random string. Every POST `/mcp` then needs one of:
- `Authorization: Bearer <key>` (clients that allow custom headers)
- `X-API-Key: <key>`
- `https://<host>/mcp?key=<key>`: for Claude/Grok custom connectors, which only take a URL. Treat that URL as a secret.
Unset = open (fine locally). `/healthz` stays open.

## Run
```
python3 test_server.py                    # 11 tests, fixture mode
python3 server.py --http                  # http://127.0.0.1:8789/mcp
DUFFEL_ACCESS_TOKEN=duffel_test_... python3 server.py --http
```

## Not done yet
- Live Duffel TEST run passed end-to-end (see `live_transcript.json`, `live_run.py`). Note: after a change, Duffel's order total was original + change_total (343.19), not the quoted new_total_amount (318.19); show both to users.
- Passenger data passes through to Duffel; nothing is stored here.
- Not in scope: AwardWallet parse, calendar update, seats/bags (`return_available_services`), hold payment via `POST /air/payments`.
- `render.yaml` is written but **not deployed**.
