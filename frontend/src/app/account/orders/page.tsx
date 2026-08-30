import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { SiteHeader } from "@/components/site-header";
import type { OrderSummary } from "@/lib/account-types";
import { authorizedFetch, getSessionUser } from "@/lib/server-auth";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Your orders" };

export default async function OrdersPage() {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/account/orders");
  const orders = (await authorizedFetch<OrderSummary[]>("orders")) ?? [];

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode="560038" />
      <main className={styles.orderHistoryPage}>
        <header>
          <h1>Your orders</h1>
          <p>Receipts and payment state from the authoritative commerce core.</p>
        </header>
        {orders.length ? (
          <div className={styles.orderHistoryList}>
            {orders.map((order) => (
              <Link href={`/orders/${order.id}`} key={order.id}>
                <div>
                  <strong>{order.public_number}</strong>
                  <time dateTime={order.created_at}>
                    {new Date(order.created_at).toLocaleString("en-IN")}
                  </time>
                </div>
                <span>{order.status.replaceAll("_", " ")}</span>
                <b>{formatMoney(order.total_minor, order.currency)}</b>
              </Link>
            ))}
          </div>
        ) : (
          <section className={styles.emptyCart}>
            <h2>No orders yet.</h2>
            <p>Your verified purchases will appear here.</p>
            <Link href="/shop" className={styles.primaryAction}>
              Shop the collection
            </Link>
          </section>
        )}
      </main>
    </div>
  );
}
