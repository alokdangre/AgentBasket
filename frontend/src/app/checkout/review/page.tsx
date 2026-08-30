import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { CheckoutPaymentFlow } from "@/components/checkout-payment-flow";
import { SiteHeader } from "@/components/site-header";
import type { Address, Cart, Checkout } from "@/lib/account-types";
import { authorizedFetch, getSessionUser } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Review checkout" };

type CheckoutReviewPageProps = {
  searchParams: Promise<{ checkout_id?: string }>;
};

export default async function CheckoutReviewPage({ searchParams }: CheckoutReviewPageProps) {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/checkout/review");
  const { checkout_id: checkoutId } = await searchParams;
  const validCheckoutId =
    checkoutId && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(checkoutId) ? checkoutId : null;
  const [cart, addressPayload, preparedCheckout] = await Promise.all([
    authorizedFetch<Cart>("cart"),
    authorizedFetch<{ addresses: Address[] }>("me/addresses"),
    validCheckoutId ? authorizedFetch<Checkout>(`checkouts/${validCheckoutId}`) : null,
  ]);
  if (!cart?.items.length) redirect("/cart");

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode={addressPayload?.addresses[0]?.postal_code ?? "560038"} />
      <main className={styles.reviewPage}>
        <header>
          <h1>Review checkout</h1>
          <p>
            {preparedCheckout
              ? "Ember prepared this exact quote. You still control approval and payment."
              : "Confirm the delivery destination and exact cart before payment."}
          </p>
        </header>
        <CheckoutPaymentFlow
          cart={cart}
          addresses={addressPayload?.addresses ?? []}
          initialCheckout={preparedCheckout}
        />
      </main>
    </div>
  );
}
