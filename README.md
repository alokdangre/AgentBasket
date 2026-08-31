# AgentBasket

Agentic-commerce merchant application for a hybrid local cafe and specialty roastery.

The merchant app now contains:

- deterministic catalog, inventory, checkout, order and payment services under
  [`backend`](backend/);
- a responsive Next.js storefront, customer account, cart and merchant console under
  [`frontend`](frontend/);
- Razorpay test-mode checkout with server-side signature and capture verification; and
- Ask Ember, a LangGraph shopping agent for recommendations, cart changes, exact checkout
  preparation and AP2-gated in-chat Razorpay payment.

Run the storefront with `cd frontend && npm install && npm run dev` after starting the FastAPI core.
