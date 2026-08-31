"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

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

type Pending = "refresh" | "challenge" | "approve" | "payment" | "verify" | null;
type Notice = { tone: "neutral" | "success" | "error"; text: string } | null;

const STAGES = ["Quote", "Authorize", "Pay", "Receipt"] as const;

function formatDeadline(value: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Kolkata",
  }).format(new Date(value));
}

function etaLabel(minimum?: number | null, maximum?: number | null): string {
  if (minimum == null || maximum == null) return "Confirmed at fulfillment";
  if (maximum < 60) return `${minimum}–${maximum} min`;
  return `${Math.round(minimum / 60)}–${Math.round(maximum / 60)} hr`;
}

export function AgentCheckoutPayment({
  checkout,
  disabled,
  onPaid,
}: {
  checkout: Checkout;
  disabled: boolean;
  onPaid: () => void;
}) {
  const [challengeKey, setChallengeKey] = useState(() => crypto.randomUUID());
  const [approvalKey, setApprovalKey] = useState(() => crypto.randomUUID());
  const [liveCheckout, setLiveCheckout] = useState(checkout);
  const [challenge, setChallenge] = useState<AP2Challenge | null>(null);
  const [approval, setApproval] = useState<AP2Approval | null>(null);
  const [receipt, setReceipt] = useState<OrderReceipt | null>(null);
  const [evidence, setEvidence] = useState<AP2Evidence | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const refreshState = useCallback(
    async (announce: boolean, signal?: AbortSignal) => {
      setPending("refresh");
      if (announce) {
        setNotice({ tone: "neutral", text: "Checking authoritative payment state…" });
      }
      try {
        const [checkoutResponse, evidenceResponse] = await Promise.all([
          fetch(`/api/commerce/checkouts/${checkout.id}`, { cache: "no-store", signal }),
          fetch(`/api/commerce/checkouts/${checkout.id}/ap2/evidence`, {
            cache: "no-store",
            signal,
          }),
        ]);
        let refreshedCheckout: Checkout | null = null;
        if (checkoutResponse.ok) {
          refreshedCheckout = (await checkoutResponse.json()) as Checkout;
          setLiveCheckout(refreshedCheckout);
        }
        if (evidenceResponse.ok) {
          const restored = (await evidenceResponse.json()) as AP2Evidence;
          setEvidence(restored);
          if (announce) {
            const terminalStatus =
              refreshedCheckout?.status === "expired" || refreshedCheckout?.status === "canceled"
                ? refreshedCheckout.status
                : null;
            setNotice(
              terminalStatus
                ? {
                    tone: "error",
                    text: `This quote is ${terminalStatus}. Prepare a fresh checkout before paying.`,
                  }
                : restored.receipts.length >= 2
                  ? {
                      tone: "success",
                      text: "Captured payment and both AP2 receipts are verified.",
                    }
                  : {
                      tone: "success",
                      text: "Your AP2 authorization is verified and ready to pay.",
                    },
            );
          }
        } else if (announce) {
          setNotice({
            tone: "neutral",
            text: "No captured payment was found yet. A dismissed or failed attempt is safe to retry.",
          });
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (announce) {
          setNotice({
            tone: "error",
            text: "Payment state could not be refreshed. No fulfillment state was changed.",
          });
        }
      } finally {
        if (!signal?.aborted) setPending(null);
      }
    },
    [checkout.id],
  );

  useEffect(() => {
    const controller = new AbortController();
    const task = window.setTimeout(() => void refreshState(false, controller.signal), 0);
    return () => {
      window.clearTimeout(task);
      controller.abort();
    };
  }, [refreshState]);

  async function requestChallenge() {
    setPending("challenge");
    setNotice(null);
    try {
      const response = await fetch(`/api/commerce/checkouts/${liveCheckout.id}/ap2/challenge`, {
        method: "POST",
        headers: { "Idempotency-Key": challengeKey },
      });
      const result = (await response.json()) as AP2Challenge & ApiError;
      if (!response.ok) {
        setNotice({
          tone: "error",
          text: apiErrorMessage(result, "Could not prepare AP2 approval evidence."),
        });
        await refreshState(false);
        return;
      }
      setChallenge(result);
      setNotice({ tone: "neutral", text: "Review the exact terms. Payment has not started." });
    } catch {
      setNotice({
        tone: "error",
        text: "The approval service is unavailable. No payment was started.",
      });
    } finally {
      setPending(null);
    }
  }

  async function verifyPayment(result: RazorpaySuccess, session: RazorpaySession) {
    setPending("verify");
    setNotice({ tone: "neutral", text: "Verifying capture and issuing signed AP2 receipts…" });
    try {
      const response = await fetch("/api/commerce/payments/razorpay/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ checkout_id: session.checkout_id, ...result }),
      });
      const order = (await response.json()) as OrderReceipt & ApiError;
      if (!response.ok) {
        setNotice({
          tone: "error",
          text: apiErrorMessage(order, "Payment was not verified; fulfillment remains blocked."),
        });
        return;
      }
      const evidenceResponse = await fetch(
        `/api/commerce/checkouts/${liveCheckout.id}/ap2/evidence`,
        { cache: "no-store" },
      );
      const ap2 = (await evidenceResponse.json()) as AP2Evidence & ApiError;
      setReceipt(order);
      setLiveCheckout((current) => ({ ...current, status: "completed" }));
      if (evidenceResponse.ok) setEvidence(ap2);
      setNotice({ tone: "success", text: "Captured payment verified. Your order is confirmed." });
      onPaid();
    } catch {
      setNotice({
        tone: "error",
        text: "Confirmation was interrupted. Use Check payment status; the signed webhook can reconcile it.",
      });
    } finally {
      setPending(null);
    }
  }

  async function openRazorpay() {
    if (!approval && (evidence?.mandates.length ?? 0) < 2) return;
    setPending("payment");
    setNotice({ tone: "neutral", text: "Creating a Razorpay order for the authorized amount…" });
    try {
      const sessionRequest = fetch(
        `/api/commerce/checkouts/${liveCheckout.id}/payment-session`,
        { method: "POST" },
      );
      await loadRazorpayCheckout();
      const response = await sessionRequest;
      const session = (await response.json()) as RazorpaySession & ApiError;
      if (!response.ok) {
        setNotice({
          tone: "error",
          text: apiErrorMessage(session, "Could not create the Razorpay session."),
        });
        await refreshState(false);
        return;
      }
      if (
        session.amount_minor !== liveCheckout.total_minor ||
        session.currency !== liveCheckout.currency
      ) {
        setNotice({
          tone: "error",
          text: "Razorpay session terms did not match the AP2-approved quote. Payment was blocked.",
        });
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
            setNotice({
              tone: "neutral",
              text: "Payment window closed. Your verified authorization remains safe to retry.",
            }),
        },
        handler: (result) => void verifyPayment(result, session),
      });
      razorpay.on?.("payment.failed", (failure) => {
        setNotice({
          tone: "error",
          text:
            failure.error?.description ??
            "Razorpay rejected the attempt. The order was not marked paid.",
        });
      });
      razorpay.open();
      setLiveCheckout((current) => ({ ...current, status: "payment_pending" }));
      setNotice({ tone: "neutral", text: "Razorpay opened for the authorized amount." });
    } catch {
      setNotice({
        tone: "error",
        text: "Razorpay could not load. No browser payment was completed.",
      });
    } finally {
      setPending(null);
    }
  }

  async function approveTerms() {
    if (!challenge) return;
    setPending("approve");
    setNotice({ tone: "neutral", text: "Verifying the closed Checkout and Payment Mandates…" });
    try {
      const response = await fetch(`/api/commerce/checkouts/${liveCheckout.id}/ap2/approve`, {
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
          expected_total_minor: liveCheckout.total_minor,
          currency: liveCheckout.currency,
          quote_version: liveCheckout.quote_version,
        }),
      });
      const result = (await response.json()) as AP2Approval & ApiError;
      if (!response.ok) {
        setNotice({
          tone: "error",
          text: apiErrorMessage(result, "AP2 approval was rejected. No payment started."),
        });
        await refreshState(false);
        return;
      }
      setApproval(result);
      setLiveCheckout((current) => ({ ...current, status: "approved" }));
      setNotice({
        tone: "success",
        text: "Both AP2 mandates are verified. Razorpay has not opened yet.",
      });
    } catch {
      setNotice({
        tone: "error",
        text: "AP2 verification was interrupted. No payment session was started.",
      });
    } finally {
      setPending(null);
    }
  }

  function prepareFreshChallenge() {
    setChallenge(null);
    setApproval(null);
    setEvidence(null);
    setChallengeKey(crypto.randomUUID());
    setApprovalKey(crypto.randomUUID());
    setNotice({ tone: "neutral", text: "The previous terms were discarded. Prepare a fresh review." });
  }

  const mandateCount = approval?.mandates.length ?? evidence?.mandates.length ?? 0;
  const authorized = mandateCount >= 2;
  const receiptCount = evidence?.receipts.length ?? 0;
  const confirmed = Boolean(receipt) || receiptCount >= 2;
  const selectedFulfillment = liveCheckout.fulfillment_options.find((option) => option.selected);
  const terminal = liveCheckout.status === "expired" || liveCheckout.status === "canceled";
  const activeStage = confirmed ? 3 : authorized ? 2 : challenge ? 1 : 0;
  const recoveredOrderId = evidence?.receipts.find((item) => item.order_id)?.order_id;
  const orderId = receipt?.id ?? recoveredOrderId;

  return (
    <section className={styles.agentCheckoutArtifact} aria-busy={pending !== null}>
      <ol className={styles.agentPurchaseProgress} aria-label="Purchase progress">
        {STAGES.map((stage, index) => (
          <li
            key={stage}
            data-state={index < activeStage ? "complete" : index === activeStage ? "active" : "next"}
          >
            <span>{index + 1}</span>
            {stage}
          </li>
        ))}
      </ol>

      {confirmed ? (
        <div className={styles.agentPaymentReceipt}>
          <span>Order confirmed</span>
          <strong>{receipt?.public_number ?? "Payment captured"}</strong>
          <p>
            {formatMoney(liveCheckout.total_minor, liveCheckout.currency)} captured ·{" "}
            {receiptCount ? `${receiptCount}/2 signed AP2 receipts` : "signed AP2 receipts reconciling"}
          </p>
          {orderId ? <Link href={`/orders/${orderId}`}>Open receipt and timeline</Link> : null}
        </div>
      ) : (
        <>
          <header className={styles.agentCheckoutHeader}>
            <div>
              <span>Agent-prepared quote</span>
              <strong>{formatMoney(liveCheckout.total_minor, liveCheckout.currency)}</strong>
            </div>
            <small>Quote v{liveCheckout.quote_version}</small>
          </header>

          <div className={styles.agentCheckoutLines}>
            {liveCheckout.lines.map((line) => (
              <div key={line.id}>
                <span>
                  {line.quantity} × {line.product_name}
                  <small>{line.variant_name}</small>
                  {line.modifiers.length ? (
                    <small>{line.modifiers.map((modifier) => modifier.name).join(", ")}</small>
                  ) : null}
                </span>
                <strong>{formatMoney(line.line_total_minor, liveCheckout.currency)}</strong>
              </div>
            ))}
          </div>

          <dl className={styles.agentCheckoutTotals}>
            <div>
              <dt>Subtotal</dt>
              <dd>{formatMoney(liveCheckout.subtotal_minor, liveCheckout.currency)}</dd>
            </div>
            <div>
              <dt>Delivery</dt>
              <dd>{formatMoney(liveCheckout.delivery_minor, liveCheckout.currency)}</dd>
            </div>
            {liveCheckout.discount_minor ? (
              <div>
                <dt>Discount</dt>
                <dd>−{formatMoney(liveCheckout.discount_minor, liveCheckout.currency)}</dd>
              </div>
            ) : null}
            <div className={styles.agentCheckoutTotal}>
              <dt>Total {liveCheckout.tax_included ? "(tax included)" : ""}</dt>
              <dd>{formatMoney(liveCheckout.total_minor, liveCheckout.currency)}</dd>
            </div>
          </dl>

          <div className={styles.agentFulfillmentSummary}>
            <div>
              <span>Fulfillment</span>
              <strong>
                {challenge?.display.fulfillment.title ?? selectedFulfillment?.title ?? liveCheckout.fulfillment_type}
              </strong>
            </div>
            <div>
              <span>Destination</span>
              <strong>{challenge?.display.fulfillment.postal_code ?? liveCheckout.postal_code ?? "Pickup"}</strong>
            </div>
            <div>
              <span>ETA</span>
              <strong>
                {etaLabel(
                  challenge?.display.fulfillment.eta_min_minutes ?? selectedFulfillment?.eta_min_minutes,
                  challenge?.display.fulfillment.eta_max_minutes ?? selectedFulfillment?.eta_max_minutes,
                )}
              </strong>
            </div>
          </div>

          <p className={styles.agentQuoteExpiry}>
            Inventory is reserved until {formatDeadline(liveCheckout.expires_at)}. The agent cannot
            authorize or press Pay.
          </p>

          {terminal ? (
            <div className={styles.agentPaymentRecovery}>
              <strong>This quote is {liveCheckout.status}.</strong>
              <span>Ask Ember to prepare a fresh checkout with current price and inventory.</span>
            </div>
          ) : !challenge && !authorized ? (
            <button
              type="button"
              className={styles.agentPaymentPrimary}
              disabled={disabled || pending !== null}
              onClick={() => void requestChallenge()}
            >
              {pending === "challenge" ? "Preparing exact terms…" : "Review AP2 authorization"}
            </button>
          ) : !authorized && challenge ? (
            <div className={styles.agentAp2Approval}>
              <div className={styles.agentMandatePair}>
                <div>
                  <span>Checkout mandate</span>
                  <strong>Items · destination · total</strong>
                </div>
                <div>
                  <span>Payment mandate</span>
                  <strong>One exact Razorpay payment</strong>
                </div>
              </div>
              <div className={styles.agentEvidenceHash}>
                <span>Merchant-signed checkout hash</span>
                <code title={challenge.checkout_hash}>{challenge.checkout_hash}</code>
              </div>
              <p>Consent terms expire {formatDeadline(challenge.expires_at)}.</p>
              <button
                type="button"
                className={styles.agentPaymentPrimary}
                disabled={disabled || pending !== null}
                onClick={() => void approveTerms()}
              >
                {pending === "approve"
                  ? "Verifying both mandates…"
                  : `Authorize ${formatMoney(liveCheckout.total_minor, liveCheckout.currency)}`}
              </button>
              <button
                type="button"
                className={styles.agentPaymentSecondary}
                disabled={disabled || pending !== null}
                onClick={prepareFreshChallenge}
              >
                Discard these terms
              </button>
            </div>
          ) : (
            <div className={styles.agentAuthorizedPayment}>
              <div>
                <span>AP2 authorization verified</span>
                <strong>{mandateCount}/2 signed mandates</strong>
              </div>
              <p>Razorpay receives only this authorized amount. The backend still verifies capture.</p>
              <button
                type="button"
                className={styles.agentPaymentPrimary}
                disabled={disabled || pending !== null}
                onClick={() => void openRazorpay()}
              >
                {pending === "payment"
                  ? "Opening Razorpay…"
                  : `Pay ${formatMoney(liveCheckout.total_minor, liveCheckout.currency)}`}
              </button>
              <button
                type="button"
                className={styles.agentPaymentSecondary}
                disabled={disabled || pending !== null}
                onClick={() => void refreshState(true)}
              >
                {pending === "refresh" ? "Checking…" : "Check payment status"}
              </button>
            </div>
          )}
        </>
      )}

      {notice ? (
        <p className={styles.agentPaymentStatus} data-tone={notice.tone} role="status">
          {notice.text}
        </p>
      ) : null}
    </section>
  );
}
