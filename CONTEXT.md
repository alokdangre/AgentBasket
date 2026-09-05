# AgentBasket Commerce Context

AgentBasket is a merchant-owned commerce system in which conversational and external agents may
help a customer shop, while deterministic services retain authority over every state change and
payment.

## Language

**Shopping Agent**:
The customer-facing assistant that discovers products and proposes bounded commerce actions.
_Avoid_: Buyer bot, autonomous buyer

**Agent Action Policy**:
The deterministic authorization decision that selects a phase-specific tool surface and budgets a run.
_Avoid_: Agent permission prompt, model guardrail

**Trusted Surface**:
The signed-in user interface where a customer reviews exact terms and performs approval or payment.
_Avoid_: Agent checkout, payment tool

**Prepared Checkout**:
An authoritative, expiring quote that is ready for customer review but is not approved or paid.
_Avoid_: Completed checkout, order

**Preference Memory**:
A typed, customer-and-merchant-scoped fact explicitly saved for advisory personalization.
_Avoid_: Conversation memory, profile instruction

**Run Checkpoint**:
The durable record of an agent run's policy, completed tools, verified mutations, and recovery state.
_Avoid_: Chat history, model memory

**External Agent Handoff**:
An opaque UCP checkout session that preserves selected variants and requires a signed-in customer to continue.
_Avoid_: Agent payment, autonomous checkout

**Scheduled Purchase Draft**:
An inert proposal whose bounded recurring authority must be granted separately on the Trusted Surface.
_Avoid_: Subscription, active schedule

**Audit Chain**:
An append-only, sequenced stream of security and commerce events linked by canonical hashes.
_Avoid_: Debug log, model trace

## Relationships

- A **Shopping Agent** receives exactly one **Agent Action Policy** per turn.
- An **Agent Action Policy** permits at most one mutating tool call per turn.
- A **Shopping Agent** may create a **Prepared Checkout** but cannot approve or pay it.
- A **Prepared Checkout** moves to approval and payment only through the **Trusted Surface**.
- A **Preference Memory** belongs to exactly one customer and one merchant and never grants authority.
- A **Run Checkpoint** records verified effects so retries do not repeat a mutation.
- An **External Agent Handoff** imports into the same customer-owned cart and checkout services as the storefront.
- A **Scheduled Purchase Draft** becomes executable only after separate passkey, AP2, and provider authorization.
- Each security-relevant transition appends an event to an **Audit Chain**.

## Example dialogue

> **Dev:** "Can the **Shopping Agent** use a saved **Preference Memory** to prepare a latte checkout?"
> **Domain expert:** "It may use oat milk to improve the recommendation, but the current turn and the
> **Agent Action Policy** must still explicitly authorize the **Prepared Checkout**. Approval and payment
> remain on the **Trusted Surface**."

## Flagged ambiguities

- "Memory" previously meant chat history, recovery state, and lasting preferences; use **Preference Memory**
  for personalization and **Run Checkpoint** for recovery.
- "Checkout" previously implied both a quote and a paid purchase; **Prepared Checkout** is neither an
  approval nor an order.
- "Agent checkout" previously implied external agents could complete payment; use **External Agent Handoff**
  for UCP sessions that always escalate to the customer.
