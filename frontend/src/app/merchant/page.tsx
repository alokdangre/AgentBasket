import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { BagIcon, BoxIcon, GridIcon } from "@/components/icons";
import { LogoutButton } from "@/components/logout-button";
import { OperationsConsole } from "@/components/operations-console";
import type { OperationsDashboard } from "@/lib/account-types";
import { authorizedFetch, getSessionUser, hasMerchantRole } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";

export const metadata: Metadata = { title: "Merchant operations" };

export default async function MerchantPage() {
  const user = await getSessionUser();
  if (!user) redirect("/account/sign-in?next=/merchant");
  if (!hasMerchantRole(user)) redirect("/account");
  const dashboard = await authorizedFetch<OperationsDashboard>("merchant/operations/dashboard");
  if (!dashboard) redirect("/account/sign-in?next=/merchant");

  return (
    <div className={styles.operationsShell}>
      <header className={styles.operationsHeader}>
        <Link href="/" className={styles.operationsBrand}>
          Ember &amp; Leaf <small>Bengaluru</small>
        </Link>
        <nav aria-label="Merchant navigation">
          <Link href="/">Storefront</Link>
          <Link href="/merchant" aria-current="page">
            Operations
          </Link>
        </nav>
        <div>
          <span>{user.full_name}</span>
          <LogoutButton showIcon={false} />
        </div>
      </header>
      <aside className={styles.operationsNav}>
        <a href="#overview" aria-current="page">
          <GridIcon /> Overview
        </a>
        <a href="#orders">
          <BagIcon /> Orders
        </a>
        <a href="#commerce">
          <GridIcon /> Protocols &amp; pay
        </a>
        <a href="#inventory">
          <BoxIcon /> Inventory
        </a>
      </aside>
      <main id="overview" className={styles.operationsMain}>
        <OperationsConsole
          initialDashboard={dashboard}
          canAdjustInventory={user.role === "merchant_admin"}
        />
      </main>
    </div>
  );
}
