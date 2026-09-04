"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  apiErrorMessage,
  type ApiError,
  type UcpCheckoutClaim,
  type UcpCheckoutHandoff,
} from "@/lib/account-types";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";

export function UcpCheckoutHandoffView({
  handoff,
  expiresLabel,
  unavailable,
}: {
  handoff: UcpCheckoutHandoff;
  expiresLabel: string;
  unavailable: boolean;
}) {
  const router = useRouter();
  const [claim, setClaim] = useState<UcpCheckoutClaim | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const total = handoff.totals.find((row) => row.type === "total")?.amount ?? 0;

  async function continueCheckout() {
    setPending(true);
    setMessage(null);
    try {
      const response = await fetch(
        `/api/commerce/ucp/checkout-handoffs/${handoff.id}/claim`,
        { method: "POST" },
      );
      const result = (await response.json()) as UcpCheckoutClaim & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not continue this agent checkout."));
        return;
      }
      setClaim(result);
      window.dispatchEvent(new Event("cart:updated"));
      if (!result.configuration_required.length) {
        router.push(result.next_url);
        router.refresh();
        return;
      }
      setMessage(
        result.imported_item_count
          ? "Ready items were added. Personalize the remaining drinks before checkout."
          : "These drinks need your choices before they can be added.",
      );
    } catch {
      setMessage("The commerce service is unavailable. Nothing was added to your cart.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.handoffLayout}>
      <section className={styles.handoffMain}>
        <span className={styles.handoffEyebrow}>UCP checkout handoff</span>
        <h1>Review what your agent selected.</h1>
        <p>
          The agent prepared these catalog choices. AgentBasket will recalculate inventory,
          delivery, taxes and the final total before you approve or pay.
        </p>
        <div className={styles.handoffLines}>
          {handoff.line_items.map((line) => (
            <article key={line.id}>
              <span>{line.quantity} ×</span>
              <div>
                <h2>{line.item.title}</h2>
                <small>Variant {line.item.id}</small>
              </div>
              <strong>{formatMoney(line.totals[0]?.amount ?? 0, handoff.currency)}</strong>
            </article>
          ))}
        </div>
        {claim?.configuration_required.length ? (
          <div className={styles.handoffConfigure}>
            <h2>Choose drink options</h2>
            <p>Milk, sweetness and temperature always remain buyer-controlled.</p>
            {claim.configuration_required.map((item) => (
              <Link
                key={item.variant_id}
                href={`${item.shop_url}&ucp_handoff=${encodeURIComponent(handoff.id)}`}
              >
                Configure {item.product_name}
              </Link>
            ))}
            {claim.imported_item_count ? (
              <Link href={`/cart?ucp_handoff=${encodeURIComponent(handoff.id)}`}>
                Review imported items
              </Link>
            ) : null}
          </div>
        ) : null}
      </section>
      <aside className={styles.handoffSummary}>
        <span>Agent estimate</span>
        <strong>{formatMoney(total, handoff.currency)}</strong>
        <p>Base item prices only. The authoritative checkout may add delivery or modifiers.</p>
        <dl>
          <div>
            <dt>Expires</dt>
            <dd>{expiresLabel}</dd>
          </div>
          <div>
            <dt>Payment</dt>
            <dd>AP2 review → Razorpay</dd>
          </div>
        </dl>
        <button type="button" disabled={pending || unavailable} onClick={continueCheckout}>
          {unavailable ? "Handoff expired" : pending ? "Importing…" : "Continue securely"}
        </button>
        <small>
          No agent can approve the quote, open Razorpay or mark the order paid. Those actions stay
          on this trusted surface.
        </small>
        {message ? (
          <p role="status" aria-live="polite">
            {message}
          </p>
        ) : null}
      </aside>
    </div>
  );
}
