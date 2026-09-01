import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { AddressManager } from "@/components/address-manager";
import { HomeIcon, UserIcon } from "@/components/icons";
import { LogoutButton } from "@/components/logout-button";
import { ProfileForm } from "@/components/profile-form";
import { PasskeyManager } from "@/components/passkey-manager";
import { SiteHeader } from "@/components/site-header";
import type { Address, Passkey } from "@/lib/account-types";
import { authorizedFetch, getSessionUser, hasMerchantRole } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "My account" };

export default async function AccountPage() {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/account");
  const [addressPayload, passkeyPayload] = await Promise.all([
    authorizedFetch<{ addresses: Address[] }>("me/addresses"),
    authorizedFetch<{ passkeys: Passkey[] }>("me/passkeys"),
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
          <section id="purchase-security" className={styles.accountSection}>
            <header>
              <h2>Purchase security</h2>
              <p>
                A passkey makes this browser the non-agentic Trusted Surface for exact AP2 terms.
              </p>
            </header>
            <PasskeyManager initialPasskeys={passkeyPayload?.passkeys ?? []} />
          </section>
        </div>
      </main>
    </div>
  );
}
