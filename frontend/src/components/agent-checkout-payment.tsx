"use client";

import Link from "next/link";
import { useState } from "react";

import {
  apiErrorMessage,
  type AP2Approval,
  type AP2Challenge,
  type AP2Evidence,
  type ApiError,
  type Checkout,
  type OrderReceipt,
  type RazorpaySession,
} from "@/lib/account-types";
import { loadRazorpayCheckout, type RazorpaySuccess } from "@/lib/razorpay-checkout";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type Pending = "challenge" | "approve" | "payment" | "verify" | null;

export function AgentCheckoutPayment({
  checkout,
  disabled,
  onPaid,
}: {
  checkout: Checkout;
  disabled: boolean;
  onPaid: () => void;
}) {
  const [challengeKey] = useState(() => crypto.randomUUID());
  const [approvalKey] = useState(() => crypto.randomUUID());
  const [challenge, setChallenge] = useState<AP2Challenge | null>(null);
  const [approval, setApproval] = useState<AP2Approval | null>(null);
  const [receipt, setReceipt] = useState<OrderReceipt | null>(null);
  const [evidence, setEvidence] = useState<AP2Evidence | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function requestChallenge() {
    setPending("challenge");
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/checkouts/${checkout.id}/ap2/challenge`, {
        method: "POST",
        headers: { "Idempotency-Key": challengeKey },
      });
      const result = (await response.json()) as AP2Challenge & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not prepare AP2 approval evidence."));
        return;
      }
      setChallenge(result);
      setMessage("Review the exact terms. No payment has been started.");
    } catch {
      setMessage("The approval service is unavailable. No payment was started.");
    } finally {
      setPending(null);
    }
  }

  async function verifyPayment(result: RazorpaySuccess, session: RazorpaySession) {
    setPending("verify");
    setMessage("Verifying Razorpay capture and issuing AP2 receipts…");
    try {
      const response = await fetch("/api/commerce/payments/razorpay/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ checkout_id: session.checkout_id, ...result }),
      });
      const order = (await response.json()) as OrderReceipt & ApiError;
      if (!response.ok) {
        setMessage(
          apiErrorMessage(order, "Payment was not verified; fulfillment remains blocked."),
        );
        return;
      }
      const evidenceResponse = await fetch(
        `/api/commerce/checkouts/${checkout.id}/ap2/evidence`,
        { cache: "no-store" },
      );
      const ap2 = (await evidenceResponse.json()) as AP2Evidence & ApiError;
      setReceipt(order);
      if (evidenceResponse.ok) setEvidence(ap2);
      setMessage("Captured payment verified. Your order is confirmed.");
      onPaid();
    } catch {
      setMessage(
        "Confirmation was interrupted. The signed webhook can still reconcile this order.",
      );
    } finally {
      setPending(null);
    }
  }

  async function openRazorpay() {
    setPending("payment");
    setMessage("Creating one payment session for the verified mandates…");
    try {
      const sessionRequest = fetch(`/api/commerce/checkouts/${checkout.id}/payment-session`, {
        method: "POST",
      });
      await loadRazorpayCheckout();
      const response = await sessionRequest;
      const session = (await response.json()) as RazorpaySession & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(session, "Could not create the Razorpay session."));
        return;
      }
      if (!window.Razorpay) throw new Error("Razorpay Checkout is unavailable");
      const razorpay = new window.Razorpay({
        key: session.key_id,
        amount: session.amount_minor,
        currency: session.currency,
        name: session.merchant_name,
        description: session.description,
        order_id: session.provider_order_id,
        prefill: {
          name: session.customer_name,
          email: session.customer_email,
          ...(session.customer_phone ? { contact: session.customer_phone } : {}),
        },
        theme: { color: "#df4814" },
        retry: { enabled: true },
        modal: {
          ondismiss: () =>
            setMessage("Payment window closed. The verified mandates remain safely retryable."),
        },
        handler: (result) => void verifyPayment(result, session),
      });
      razorpay.on?.("payment.failed", (failure) => {
        setMessage(
          failure.error?.description ??
            "Razorpay rejected the attempt. The order was not marked paid.",
        );
      });
      razorpay.open();
      setMessage("Razorpay opened for the AP2-approved amount.");
    } catch {
      setMessage("Razorpay could not load. No browser payment was completed.");
    } finally {
      setPending(null);
    }
  }

  async function approveAndPay() {
    if (!challenge) return;
    setPending("approve");
    setMessage("Verifying the closed Checkout and Payment Mandates…");
    try {
      const response = await fetch(`/api/commerce/checkouts/${checkout.id}/ap2/approve`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": approvalKey,
        },
        body: JSON.stringify({
          challenge_id: challenge.id,
          nonce: challenge.nonce,
          checkout_hash: challenge.checkout_hash,
          display_sha256: challenge.display_sha256,
          expected_total_minor: checkout.total_minor,
          currency: checkout.currency,
          quote_version: checkout.quote_version,
        }),
      });
      const result = (await response.json()) as AP2Approval & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "AP2 approval was rejected. No payment started."));
        return;
      }
      setApproval(result);
      await openRazorpay();
    } catch {
      setMessage("AP2 verification was interrupted. No payment session was started.");
    } finally {
      setPending(null);
    }
  }

  if (receipt) {
    return (
      <div className={styles.agentPaymentReceipt}>
        <span>Order confirmed</span>
        <strong>{receipt.public_number}</strong>
        <p>
          {formatMoney(receipt.total_minor, receipt.currency)} captured · {evidence?.receipts.length ?? 0}
          /2 AP2 receipts available
        </p>
        <Link href={`/orders/${receipt.id}`}>Open receipt and timeline</Link>
      </div>
    );
  }

  return (
    <div className={styles.agentCheckoutArtifact}>
      <span>Exact agent checkout</span>
      <strong>{formatMoney(checkout.total_minor, checkout.currency)}</strong>
      <div className={styles.agentCheckoutLines}>
        {checkout.lines.map((line) => (
          <span key={line.id}>
            {line.quantity} × {line.product_name}
          </span>
        ))}
      </div>
      <p>
        Reserved until{" "}
        {new Date(checkout.expires_at).toLocaleTimeString("en-IN", {
          hour: "2-digit",
          minute: "2-digit",
        })}
        . The model cannot press either payment button.
      </p>

      {!challenge ? (
        <button
          type="button"
          disabled={disabled || pending !== null}
          onClick={() => void requestChallenge()}
        >
          {pending === "challenge" ? "Preparing AP2 terms…" : "Review AP2 approval"}
        </button>
      ) : (
        <div className={styles.agentAp2Approval}>
          <div>
            <span>Fulfillment</span>
            <strong>{challenge.display.fulfillment.title}</strong>
          </div>
          <div>
            <span>Mandates</span>
            <strong>Checkout + Payment</strong>
          </div>
          <code title={challenge.checkout_hash}>{challenge.checkout_hash}</code>
          <button
            type="button"
            disabled={disabled || pending !== null}
            onClick={() => void (approval ? openRazorpay() : approveAndPay())}
          >
            {pending
              ? "Securing checkout…"
              : approval
                ? "Open Razorpay again"
                : `Approve ${formatMoney(checkout.total_minor, checkout.currency)} & pay`}
          </button>
        </div>
      )}
      {message ? (
        <p className={styles.agentPaymentStatus} role="status">
          {message}
        </p>
      ) : null}
    </div>
  );
}
