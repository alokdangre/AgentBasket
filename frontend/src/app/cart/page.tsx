import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { CartView } from "@/components/cart-view";
import { SiteHeader } from "@/components/site-header";
import type { Cart } from "@/lib/account-types";
import { authorizedFetch, getSessionUser } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Your cart" };

export default async function CartPage() {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/cart");
  const cart = await authorizedFetch<Cart>("cart");
  if (!cart) redirect("/account/sign-in?next=/cart");

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode="560038" />
      <main className={styles.cartPage}>
        <CartView initialCart={cart} />
      </main>
    </div>
  );
}
