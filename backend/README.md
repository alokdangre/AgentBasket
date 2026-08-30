# AgentBasket commerce core

Protocol-neutral backend for a hybrid local cafe and specialty roastery. It owns the
authoritative catalog, location, inventory, pricing, checkout, reservation, order and payment
state that UCP, ACP, AP2 and Razorpay adapters will call later.

Step 3 adds customer accounts, saved addresses, one active cart per customer/merchant, and
role-gated merchant operations. Passwords use scrypt; only SHA-256 hashes of opaque session
tokens are stored. All prices and computed totals remain server-owned integer minor units.

Step 4 adds authenticated cart-to-checkout conversion, persisted human approval evidence,
Razorpay test-mode Orders, server-side checkout-signature verification, captured payment/order
reconciliation, idempotent raw-body webhooks, inventory consumption and customer receipts.

Step 5 adds the Google ADK-powered Ask Ember agent with persistent conversations, deterministic
catalog recommendations, identity-bound cart tools, exact checkout preparation, idempotent runs
and tool-level audit records. The agent has no approval or payment tool.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
docker compose up -d postgres
.venv/bin/alembic upgrade head
.venv/bin/python -m app.db.seed
.venv/bin/uvicorn app.main:app --reload
```

OpenAPI is available at `http://localhost:8000/docs`.

The versioned API includes:

- `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`;
- `GET/PATCH /api/v1/me` and address CRUD under `/api/v1/me/addresses`;
- `GET /api/v1/cart` and cart-item mutations under `/api/v1/cart/items`;
- merchant dashboards, orders and inventory under `/api/v1/merchant/operations`;
- checkout approval and payment sessions under `/api/v1/checkouts`;
- `POST /api/v1/payments/razorpay/verify` and `POST /api/v1/webhooks/razorpay`;
- customer receipts under `/api/v1/orders`; and
- authenticated Ask Ember conversations and messages under `/api/v1/agent/conversations`.

Set the development-only `MERCHANT_ADMIN_*` values before running the seed command to create
the first merchant administrator. Do not reuse those example credentials outside local setup.

For Razorpay, configure test-mode `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and a separate
`RAZORPAY_WEBHOOK_SECRET`. Subscribe the test webhook to `payment.captured`, `payment.failed`
and `order.paid`, and enable automatic capture in the Razorpay Dashboard. The secret keys are
server-only; Standard Checkout receives only the public key ID and the exact provider order
created by the backend.

For Ask Ember, set the backend-only `GOOGLE_API_KEY`. `AGENT_MODEL` defaults to
`gemini-flash-latest`. Google ADK is pinned to 2.8.0 because its 2.x agent, event and session APIs
are not compatible with older releases. Live Gemini calls require your key; the automated suite
uses a deterministic fake runtime while exercising the real commerce tools and persistence.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

Tests use an isolated SQLite database. PostgreSQL remains the production database and Alembic
is the schema authority.
