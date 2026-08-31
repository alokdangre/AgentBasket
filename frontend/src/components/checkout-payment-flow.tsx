"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import {
  apiErrorMessage,
  type Address,
  type ApiError,
  type Cart,
  type Checkout,
  type CheckoutApproval,
  type OrderReceipt,
  type RazorpaySession,
} from "@/lib/account-types";
import { loadRazorpayCheckout, type RazorpaySuccess } from "@/lib/razorpay-checkout";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";

type PendingAction = "quote" | "approve" | "payment" | "verify" | null;

export function CheckoutPaymentFlow({
  cart,
  addresses,
  initialCheckout = null,
}: {
  cart: Cart;
  addresses: Address[];
  initialCheckout?: Checkout | null;
}) {
  const router = useRouter();
  const checkoutIdempotencyKey = useRef<string | null>(null);
  const [selectedAddressId, setSelectedAddressId] = useState(
    addresses.find((address) => address.is_default)?.id ?? addresses[0]?.id ?? "",
  );
  const [checkout, setCheckout] = useState<Checkout | null>(initialCheckout);
  const [approval, setApproval] = useState<CheckoutApproval | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function createQuote() {
    if (!selectedAddressId) {
      setMessage("Add and select a delivery address before checkout.");
      return;
    }
    setPending("quote");
    setMessage(null);
    checkoutIdempotencyKey.current ??= crypto.randomUUID();
    try {
      const response = await fetch("/api/commerce/checkouts/from-cart", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": checkoutIdempotencyKey.current,
        },
        body: JSON.stringify({
          merchant_slug: cart.merchant_slug,
          fulfillment_type: "local_delivery",
          address_id: selectedAddressId,
        }),
      });
      const result = (await response.json()) as Checkout & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not create the exact checkout."));
        return;
      }
      setCheckout(result);
      setMessage("Exact price and inventory are locked for this checkout.");
    } catch {
      setMessage("The service is unavailable. Try again with the same cart.");
    } finally {
      setPending(null);
    }
  }

  async function approveQuote() {
    if (!checkout) return;
    setPending("approve");
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/checkouts/${checkout.id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_total_minor: checkout.total_minor,
          quote_version: checkout.quote_version,
        }),
      });
      const result = (await response.json()) as CheckoutApproval & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not record your approval."));
        if (result.error?.code === "checkout_expired") {
          setCheckout(null);
          setApproval(null);
          checkoutIdempotencyKey.current = null;
        }
        return;
      }
      setApproval(result);
      setCheckout((current) => (current ? { ...current, status: "approved" } : current));
      setMessage("Approval recorded. No payment has been made yet.");
    } catch {
      setMessage("The service is unavailable. No payment was started.");
    } finally {
      setPending(null);
    }
  }

  async function verifyPayment(response: RazorpaySuccess, session: RazorpaySession) {
    setPending("verify");
    setMessage("Verifying the signature and captured payment with Razorpay…");
    try {
      const verification = await fetch("/api/commerce/payments/razorpay/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ checkout_id: session.checkout_id, ...response }),
      });
      const result = (await verification.json()) as OrderReceipt & ApiError;
      if (!verification.ok) {
        setMessage(
          apiErrorMessage(
            result,
            "Payment could not be verified. Your order remains blocked from fulfillment.",
          ),
        );
        return;
      }
      router.push(`/orders/${result.id}`);
      router.refresh();
    } catch {
      setMessage(
        "Confirmation was interrupted. Razorpay webhooks can still reconcile the payment; check Orders shortly.",
      );
    } finally {
      setPending(null);
    }
  }

  async function openPayment() {
    if (!checkout || !approval) return;
    setPending("payment");
    setMessage(null);
    try {
      const sessionRequest = fetch(
        `/api/commerce/checkouts/${checkout.id}/payment-session`,
        { method: "POST" },
      );
      await loadRazorpayCheckout();
      const response = await sessionRequest;
      const session = (await response.json()) as RazorpaySession & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(session, "Could not start the secure payment session."));
        if (session.error?.code === "checkout_expired") {
          setCheckout(null);
          setApproval(null);
          checkoutIdempotencyKey.current = null;
        }
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
            setMessage("Payment window closed. Nothing is fulfilled until capture is verified."),
        },
        handler: (result) => void verifyPayment(result, session),
      });
      razorpay.on?.("payment.failed", (failure) => {
        setMessage(
          failure.error?.description ??
            "Razorpay reported a failed attempt. You can safely retry this approved order.",
        );
      });
      razorpay.open();
      setMessage("Secure Razorpay Checkout opened for the approved amount.");
    } catch {
      setMessage("Razorpay Checkout could not load. No payment was created in the browser.");
    } finally {
      setPending(null);
    }
  }

  const exactCheckout = checkout;
  return (
    <div className={styles.checkoutFlow}>
      <section className={styles.checkoutMain}>
        <div className={styles.checkoutSectionHeading}>
          <div>
            <h2>Delivery address</h2>
            <p>The location and delivery fee are calculated from this address.</p>
          </div>
          <span>1</span>
        </div>
        {addresses.length ? (
          <div className={styles.checkoutAddresses}>
            {addresses.map((address) => (
              <label key={address.id} className={styles.reviewAddress}>
                <input
                  type="radio"
                  name="address"
                  value={address.id}
                  checked={selectedAddressId === address.id}
                  disabled={Boolean(exactCheckout)}
                  onChange={() => setSelectedAddressId(address.id)}
                />
                <span>
                  <strong>{address.label}</strong>
                  {address.line_one}, {address.city}, {address.region} {address.postal_code}
                </span>
              </label>
            ))}
          </div>
        ) : (
          <div className={styles.reviewMissing}>
            <p>Add a delivery address before checkout.</p>
            <Link href="/account#addresses">Add an address</Link>
          </div>
        )}
        {!exactCheckout ? (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={pending !== null || !selectedAddressId}
            onClick={createQuote}
          >
            {pending === "quote" ? "Calculating…" : "Calculate exact checkout"}
          </button>
        ) : null}

        {exactCheckout ? (
          <section className={styles.paymentGate}>
            <div className={styles.checkoutSectionHeading}>
              <div>
                <h2>Explicit payment approval</h2>
                <p>Confirm the exact amount below before Razorpay can be opened.</p>
              </div>
              <span>2</span>
            </div>
            <dl className={styles.exactTerms}>
              <div>
                <dt>Amount</dt>
                <dd>{formatMoney(exactCheckout.total_minor, exactCheckout.currency)}</dd>
              </div>
              <div>
                <dt>Quote version</dt>
                <dd>{exactCheckout.quote_version}</dd>
              </div>
              <div>
                <dt>Reserved until</dt>
                <dd>{new Date(exactCheckout.expires_at).toLocaleTimeString("en-IN")}</dd>
              </div>
            </dl>
            {!approval ? (
              <button
                type="button"
                className={styles.primaryAction}
                disabled={pending !== null}
                onClick={approveQuote}
              >
                {pending === "approve"
                  ? "Recording approval…"
                  : `Approve ${formatMoney(exactCheckout.total_minor, exactCheckout.currency)}`}
              </button>
            ) : (
              <div className={styles.approvalEvidence}>
                <strong>Approval recorded</strong>
                <span>Evidence SHA-256</span>
                <code>{approval.evidence_sha256}</code>
              </div>
            )}
          </section>
        ) : null}
      </section>

      <aside className={styles.cartSummary}>
        <h2>{exactCheckout ? "Exact checkout" : "Current cart"}</h2>
        {(exactCheckout?.lines ?? cart.items).map((item) => (
          <div key={item.id} className={styles.reviewLine}>
            <span>
              {item.quantity} × {item.product_name}
            </span>
            <strong>
              {formatMoney(item.line_total_minor, exactCheckout?.currency ?? cart.currency)}
            </strong>
          </div>
        ))}
        {exactCheckout ? (
          <>
            <div className={styles.reviewLine}>
              <span>Delivery</span>
              <strong>{formatMoney(exactCheckout.delivery_minor, exactCheckout.currency)}</strong>
            </div>
            <div className={styles.cartTotal}>
              <span>Approved total</span>
              <strong>{formatMoney(exactCheckout.total_minor, exactCheckout.currency)}</strong>
            </div>
          </>
        ) : (
          <div className={styles.cartTotal}>
            <span>Cart subtotal</span>
            <strong>{formatMoney(cart.subtotal_minor, cart.currency)}</strong>
          </div>
        )}
        {approval ? (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={pending !== null}
            onClick={openPayment}
          >
            {pending === "payment" || pending === "verify"
              ? "Securing payment…"
              : "Open secure Razorpay"}
          </button>
        ) : null}
        <p className={styles.reviewNotice}>
          Fulfillment remains blocked until the server verifies the checkout signature and a
          captured Razorpay payment. Secrets never enter the browser; the exact total originates
          from the commerce core.
        </p>
        <Link href="/cart" className={styles.secondaryAction}>
          Return to cart
        </Link>
        {message ? (
          <p className={styles.formMessage} role="status" aria-live="polite">
            {message}
          </p>
        ) : null}
      </aside>
    </div>
  );
}
