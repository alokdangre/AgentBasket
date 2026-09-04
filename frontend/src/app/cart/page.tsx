import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { CartView } from "@/components/cart-view";
import { SiteHeader } from "@/components/site-header";
import type { Cart } from "@/lib/account-types";
import { authorizedFetch, getSessionUser } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Your cart" };

type CartPageProps = {
  searchParams: Promise<{ ucp_handoff?: string }>;
};

export default async function CartPage({ searchParams }: CartPageProps) {
  const { ucp_handoff: handoffId } = await searchParams;
  const validHandoffId =
    handoffId && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(handoffId) ? handoffId : null;
  const user = await getSessionUser();
  if (!user) {
    const next = validHandoffId
      ? `/cart?ucp_handoff=${encodeURIComponent(validHandoffId)}`
      : "/cart";
    redirect(`/account/sign-in?next=${encodeURIComponent(next)}`);
  }
  const cart = await authorizedFetch<Cart>("cart");
  if (!cart) redirect("/account/sign-in?next=/cart");

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode="560038" />
      <main className={styles.cartPage}>
        <CartView
          initialCart={cart}
          checkoutHref={
            validHandoffId
              ? `/checkout/review?ucp_handoff=${encodeURIComponent(validHandoffId)}`
              : "/checkout/review"
          }
        />
      </main>
    </div>
  );
}
