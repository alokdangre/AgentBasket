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

Step 5 adds the LangGraph-powered Ask Ember agent with persistent conversations, deterministic
catalog recommendations, identity-bound cart tools, exact checkout preparation, idempotent runs
and tool-level audit records. The agent has no approval or payment tool.

Step 6 adds a human-present AP2 v0.2 gate to agent-prepared checkouts. It persists one-time consent
challenges, ES256 Checkout and Payment Mandates, trusted issuers and signed success receipts, then
opens Razorpay Standard Checkout inside the conversation only after both mandates verify.

Step 8 adds human-not-present scheduled purchases. Ember can create only an inert bounded draft;
the Trusted Surface separately passkey-authorizes open AP2 mandates, Razorpay confirms a UPI
Autopay token, and a durable worker closes and verifies the mandate chains for each exact run.

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
- authenticated Ask Ember conversations and messages under `/api/v1/agent/conversations`; and
- AP2 challenge, approval and evidence under `/api/v1/checkouts/{checkout_id}/ap2`;
- customer scheduled-purchase authorization and controls under `/api/v1/scheduled-purchases`; and
- Razorpay UPI Autopay registration under
  `/api/v1/credential-provider/razorpay-upi-autopay/{scheduled_purchase_id}`.

Set the development-only `MERCHANT_ADMIN_*` values before running the seed command to create
the first merchant administrator. Do not reuse those example credentials outside local setup.
Optional `DEMO_CUSTOMER_*` values create a non-production shopper with serviceable home/office
addresses and one deliberately unsupported postcode. The incremental seed supplies an imageless,
recommendation-ready catalog and inventory edge cases, but never fakes carts, checkouts, payments,
orders, AP2 evidence or audit events.

For Razorpay, configure test-mode `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and a separate
`RAZORPAY_WEBHOOK_SECRET`. Subscribe the test webhook to `payment.captured`, `payment.failed`,
`order.paid`, `token.confirmed`, `token.paused`, `token.cancelled`, and `token.rejected`, and
enable automatic capture in the Razorpay Dashboard. The secret keys are server-only; Standard
Checkout receives only the public key ID and the exact provider order created by the backend.

For Ask Ember, set the backend-only `GOOGLE_API_KEY`. `AGENT_MODEL` defaults to
`gemini-3.5-flash-lite`, a low-latency free-tier model suitable for the bounded commerce tool loop.
LangGraph 1.2.11 owns only the bounded model/tool loop; the existing database
owns conversations, idempotency and every commerce state. Live Gemini calls require your key; the
automated suite uses a deterministic fake runtime while exercising the real tools and persistence.

Optional LangSmith tracing uses `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and
`LANGSMITH_PROJECT`. Create the key in LangSmith **Settings → API Keys**, keep it backend-only and
restart FastAPI after changing `.env`. Inputs and outputs are hidden by default because shopping
turns can contain addresses and order data; set `LANGSMITH_HIDE_INPUTS=false` and
`LANGSMITH_HIDE_OUTPUTS=false` only with non-sensitive local test data. Trace metadata contains the
local conversation/run IDs and model name so a failed request can be correlated with the audit log.

For AP2, configure separate ES256 P-256 private keys for the merchant, Agent Provider,
Credentials Provider, and test-mode payment processor using the `AP2_*` settings in
`.env.example`. The public keys are
registered on first use. Agent-prepared checkouts fail closed when issuers are absent or do not
match the trust registry. These test issuers are not a production passkey substitute.

Generate four independent local keys with `python scripts/generate_ap2_test_keys.py` and paste
the dotenv-safe output into `.env`. Razorpay does not issue these AP2 test keys.

Human-not-present schedules additionally require a 32-byte base64url
`AP2_AUTONOMOUS_AGENT_MASTER_KEY`, Razorpay S2S Recurring/UPI Autopay activation, the recurring
token webhook events listed in `.env.example`, and a separate
`python -m app.workers.scheduled_purchases` process. Standard Checkout test UPI values alone do
not prove that recurring Test Mode token support is enabled.

Razorpay MCP is not part of the customer payment runtime. Orders API, Standard Checkout,
server-side verification and webhooks remain the payment path; the remote MCP server can later be
connected separately to a permissioned merchant-operations agent.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/alembic upgrade head --sql
```

Tests use an isolated SQLite database. PostgreSQL remains the production database and Alembic
is the schema authority.
