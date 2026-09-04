# Ember & Leaf storefront

Next.js customer storefront for AgentBasket's hybrid local café and specialty
roastery merchant.

## Run locally

```bash
cp .env.example .env.local
npm install
npm run dev
```

The storefront reads the FastAPI commerce core from
`COMMERCE_API_URL=http://127.0.0.1:8000`. When that service is unavailable,
the home/catalog foundation renders the matching demo seed so visual development
and builds remain deterministic. Set `NEXT_PUBLIC_SITE_URL` to the public HTTPS storefront origin
so JSON-LD contains canonical public URLs.

The home and shop pages render Schema.org `Organization`/`CafeOrCoffeeShop`/`OnlineStore`,
`OfferCatalog`, `Product`, and `Offer` nodes from the same catalog response used by the visible UI.
The storefront also proxies `/.well-known/ucp` from the FastAPI core so buyer agents starting from
the merchant domain can discover the public UCP catalog and redirect-checkout endpoints.

## Customer and merchant routes

- `/account/sign-in`: customer registration and customer/operator sign-in;
- `/account`: profile and saved delivery addresses;
- `/cart`: authoritative server cart and quantity controls;
- `/checkout/review`: exact cart, approval and Razorpay payment flow;
- `/checkout/handoff/[sessionId]`: trusted continuation for a UCP-selected cart;
- `/merchant`: role-gated order, inventory, protocol and payment-readiness operations;
- `/orders/[orderId]`: verified receipt and money-action audit trail; and
- `/account/orders`: customer order history.

Ask Ember is now a persistent, authenticated conversation surface available from every storefront
page. It renders catalog-grounded product results, cart changes, exact checkout handoffs and the
tool audit summary. Agent recommendations and payment controls are intentionally text-only for this
phase. It never receives the backend session token, Google API key or Razorpay secret.

The in-chat purchase surface renders Quote → AP2 Authorize → Razorpay Pay → Receipt as separate
human actions. It shows every line/modifier, authoritative totals, destination, ETA and expiry;
validates the provider-session amount against the approved quote; and rehydrates checkout, mandate
and receipt evidence after panel close, reload or webhook reconciliation. Closing the chat hides it
without destroying this payment state.

For human-not-present purchases, Ember creates only a draft. The account page is the non-agentic
Trusted Surface where the customer reviews the fixed products, destination, cadence and budgets,
approves the delegation with a passkey, completes one UPI Autopay registration when required, and
can later pause, resume or revoke it. The worker—not the chat model—executes each bounded run.

Step 4 changes `/checkout/review` into a gated flow: calculate the exact server checkout, record
the customer's exact-amount approval, open Razorpay Standard Checkout, then verify capture on
the backend before showing the receipt. A dismissed or failed provider attempt leaves
fulfillment blocked and can be retried against the same approved order.

When `/checkout/review` carries a verified `ucp_handoff`, it uses the stronger in-app AP2 payment
component instead of the basic storefront approval. The UCP agent can choose catalog variants but
cannot approve the quote, use the passkey, open Razorpay, or assert capture.

Authentication uses a same-site, HTTP-only cookie owned by the Next.js server. The browser
never receives the commerce-core bearer token. The Next.js BFF only proxies an explicit
method-and-path allowlist for account, cart, checkout, payment, order and merchant operations.
Agent messages use the same BFF with a 40-second model timeout and per-message idempotency key.

## Checks

```bash
npm run lint
npm run typecheck
npm run build
```

Browser and visual QA are intentionally separate from these non-browser checks.
