import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { AddressManager } from "@/components/address-manager";
import { HomeIcon, UserIcon } from "@/components/icons";
import { LogoutButton } from "@/components/logout-button";
import { ProfileForm } from "@/components/profile-form";
import { PasskeyManager } from "@/components/passkey-manager";
import { ScheduledPurchaseManager } from "@/components/scheduled-purchase-manager";
import { SiteHeader } from "@/components/site-header";
import type {
  Address,
  Passkey,
  PaymentInstrument,
  ScheduledPurchaseList,
} from "@/lib/account-types";
import { authorizedFetch, getSessionUser, hasMerchantRole } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "My account" };

export default async function AccountPage() {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/account");
  const [addressPayload, passkeyPayload, instrumentPayload, schedulePayload] = await Promise.all([
    authorizedFetch<{ addresses: Address[] }>("me/addresses"),
    authorizedFetch<{ passkeys: Passkey[] }>("me/passkeys"),
    authorizedFetch<{ payment_instruments: PaymentInstrument[] }>(
      "credential-provider/payment-instruments",
    ),
    authorizedFetch<ScheduledPurchaseList>("scheduled-purchases"),
  ]);

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode={addressPayload?.addresses[0]?.postal_code ?? "560038"} />
      <div className={styles.signedInStrip}>
        <span>Signed in as</span>
        <strong>{user.full_name}</strong>
        <span>{user.email}</span>
      </div>
      <main className={styles.accountLayout}>
        <aside className={styles.accountNav}>
          <a href="#profile" aria-current="page">
            <UserIcon /> Profile
          </a>
          <a href="#addresses">
            <HomeIcon /> Addresses
          </a>
          <a href="#scheduled-purchases">Scheduled purchases</a>
          <a href="#purchase-security">Purchase security</a>
          <Link href="/cart">Cart</Link>
          <Link href="/account/orders">Orders</Link>
          {hasMerchantRole(user) ? <Link href="/merchant">Operations</Link> : null}
          <LogoutButton />
        </aside>
        <div className={styles.accountContent}>
          <section id="profile" className={styles.accountSection}>
            <header>
              <h1>Profile</h1>
              <p>Manage your personal details.</p>
            </header>
            <ProfileForm user={user} />
          </section>
          <section id="addresses" className={styles.accountSection}>
            <header>
              <h2>Saved addresses</h2>
              <p>Manage where local delivery and shipped products can arrive.</p>
            </header>
            <AddressManager initialAddresses={addressPayload?.addresses ?? []} />
          </section>
          <section id="scheduled-purchases" className={styles.accountSection}>
            <header>
              <h2>Scheduled purchases</h2>
              <p>
                Review, authorize and control Ember&apos;s bounded purchases when you are away.
              </p>
            </header>
            <ScheduledPurchaseManager
              initialSchedules={schedulePayload?.scheduled_purchases ?? []}
              addresses={addressPayload?.addresses ?? []}
              paymentInstruments={instrumentPayload?.payment_instruments ?? []}
            />
          </section>
          <section id="purchase-security" className={styles.accountSection}>
            <header>
              <h2>Purchase security</h2>
              <p>
                This browser uses your passkey for exact checkout approval and one-time schedule
                authorization. Later runs rely only on the signed bounds you accepted.
              </p>
            </header>
            <PasskeyManager initialPasskeys={passkeyPayload?.passkeys ?? []} />
          </section>
        </div>
      </main>
    </div>
  );
}
