import type { Metadata } from "next";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { SiteHeader } from "@/components/site-header";
import type { OrderReceipt } from "@/lib/account-types";
import { authorizedFetch, getSessionUser } from "@/lib/server-auth";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Order receipt" };

const eventLabels: Record<string, string> = {
  "checkout.quoted": "Exact checkout calculated",
  "checkout.approved": "Payment amount approved",
  "payment.session_created": "Razorpay order created",
  "payment.signature_rejected": "Invalid payment signature rejected",
  "payment.failed": "Payment attempt failed",
  "payment.captured": "Captured payment verified",
  "order.paid": "Order marked paid",
  "checkout.completed": "Checkout completed",
};

export default async function OrderReceiptPage({
  params,
}: {
  params: Promise<{ orderId: string }>;
}) {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in");
  const { orderId } = await params;
  const receipt = await authorizedFetch<OrderReceipt>(`orders/${orderId}`);
  if (!receipt) notFound();
  const address = receipt.fulfillment.delivery_address;

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode={receipt.fulfillment.postal_code ?? "560038"} />
      <main className={styles.receiptPage}>
        <header className={styles.receiptHeader}>
          <div>
            <p>{receipt.public_number}</p>
            <h1>{receipt.status === "paid" ? "Payment confirmed." : "Order received."}</h1>
          </div>
          <div className={styles.receiptStatus} data-status={receipt.payment.status}>
            <span>Payment</span>
            <strong>{receipt.payment.status.replaceAll("_", " ")}</strong>
          </div>
        </header>

        <div className={styles.receiptLayout}>
          <section className={styles.receiptSummary}>
            <h2>Receipt</h2>
            <dl>
              <div>
                <dt>Order total</dt>
                <dd>{formatMoney(receipt.total_minor, receipt.currency)}</dd>
              </div>
              <div>
                <dt>Fulfillment</dt>
                <dd>{receipt.fulfillment.title ?? receipt.fulfillment.type}</dd>
              </div>
              <div>
                <dt>Estimated arrival</dt>
                <dd>
                  {receipt.fulfillment.eta_min_minutes ?? "—"}–
                  {receipt.fulfillment.eta_max_minutes ?? "—"} minutes
                </dd>
              </div>
              <div>
                <dt>Razorpay payment</dt>
                <dd>{receipt.payment.provider_payment_id ?? "Pending verification"}</dd>
              </div>
            </dl>
            {address ? (
              <div className={styles.receiptAddress}>
                <h3>Delivering to</h3>
                <p>
                  {address.recipient_name}
                  <br />
                  {address.line_one}
                  {address.line_two ? `, ${address.line_two}` : ""}
                  <br />
                  {address.city}, {address.region} {address.postal_code}
                </p>
              </div>
            ) : null}
            <div className={styles.receiptActions}>
              <Link href="/account/orders" className={styles.primaryAction}>
                View all orders
              </Link>
              <Link href="/shop" className={styles.secondaryAction}>
                Continue shopping
              </Link>
            </div>
          </section>

          <section className={styles.timelineSection}>
            <header>
              <h2>Money-action audit trail</h2>
              <p>Every quote, approval and provider transition recorded by the commerce core.</p>
            </header>
            <ol className={styles.timeline}>
              {receipt.timeline.map((event, index) => (
                <li key={`${event.event_type}-${event.occurred_at}-${index}`}>
                  <span />
                  <div>
                    <strong>{eventLabels[event.event_type] ?? event.event_type}</strong>
                    <time dateTime={event.occurred_at}>
                      {new Date(event.occurred_at).toLocaleString("en-IN")}
                    </time>
                    <small>{event.actor_type.replaceAll("_", " ")}</small>
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </main>
    </div>
  );
}
