import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";

import { SiteHeader } from "@/components/site-header";
import { UcpCheckoutHandoffView } from "@/components/ucp-checkout-handoff";
import type { UcpCheckoutHandoff } from "@/lib/account-types";
import { getSessionUser } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";
import storefrontStyles from "@/styles/storefront.module.css";

export const metadata: Metadata = { title: "Continue agent checkout" };

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

async function getHandoff(sessionId: string): Promise<UcpCheckoutHandoff | null> {
  try {
    const response = await fetch(
      `${commerceApiUrl}/api/v1/ucp/checkout-handoffs/${encodeURIComponent(sessionId)}`,
      { cache: "no-store", signal: AbortSignal.timeout(5000) },
    );
    if (!response.ok) return null;
    return (await response.json()) as UcpCheckoutHandoff;
  } catch {
    return null;
  }
}

type HandoffPageProps = {
  params: Promise<{ sessionId: string }>;
};

export default async function HandoffPage({ params }: HandoffPageProps) {
  const { sessionId } = await params;
  if (!/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(sessionId)) notFound();

  const [user, handoff] = await Promise.all([getSessionUser(), getHandoff(sessionId)]);
  if (!handoff) notFound();
  if (!user) {
    redirect(`/account/sign-in?next=${encodeURIComponent(`/checkout/handoff/${sessionId}`)}`);
  }
  const expiresLabel = new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Kolkata",
  }).format(new Date(handoff.expires_at));

  return (
    <div className={storefrontStyles.siteShell}>
      <SiteHeader postalCode="560038" />
      <main className={styles.handoffPage}>
        <UcpCheckoutHandoffView
          handoff={handoff}
          expiresLabel={expiresLabel}
          unavailable={handoff.status === "canceled"}
        />
      </main>
    </div>
  );
}
