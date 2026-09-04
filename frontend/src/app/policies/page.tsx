import type { Metadata } from "next";

import { SiteHeader } from "@/components/site-header";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Store policies" };

export default function PoliciesPage() {
  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode="560038" />
      <main className={styles.policyPage}>
        <header>
          <span>Ember &amp; Leaf</span>
          <h1>Store policies</h1>
          <p>Plain-language terms for storefront and agent-assisted orders.</p>
        </header>
        <section id="terms">
          <h2>Terms of service</h2>
          <p>
            Catalog prices and availability are indicative until AgentBasket creates an exact,
            expiring checkout. An order is accepted only after payment capture is verified.
          </p>
        </section>
        <section id="privacy">
          <h2>Privacy</h2>
          <p>
            Public catalog and JSON-LD responses contain no buyer data. Account, delivery and
            payment evidence is used only to provide, secure and reconcile the requested order.
          </p>
        </section>
        <section id="refunds">
          <h2>Refunds and cancellations</h2>
          <p>
            A checkout can be canceled before payment processing begins. For a captured order,
            contact the merchant promptly; eligibility depends on preparation and fulfillment
            status.
          </p>
        </section>
      </main>
    </div>
  );
}
