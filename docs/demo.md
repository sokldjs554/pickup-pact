# Interview demo

The demo is intentionally product-first: an interviewer should understand the backend failure boundary before reading architecture diagrams.

## What one click proves

The browser calls the FastAPI backend at `GET /api/scenarios/{id}`. The backend reconstructs canonical event-time order, detects a domain anomaly, and returns deterministic repair commands plus evidence event IDs.

The default scenario is a late cancellation:
1. pickup was confirmed;
2. cancellation actually occurred at 12:14:10;
3. settlement and reward occurred later;
4. the cancellation arrived over the network only at 12:15:02;
5. receive-order processing would leave stale money/reward state;
6. Pickup Pact emits compensating ledger/reward commands and a CQRS rebuild.

No external provider is called. All data is synthetic.
