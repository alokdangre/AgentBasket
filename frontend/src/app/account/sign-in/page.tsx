import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { AuthForm } from "@/components/auth-form";
import { getSessionUser, hasMerchantRole } from "@/lib/server-auth";
import styles from "@/styles/account.module.css";

export const metadata: Metadata = { title: "Sign in" };

type SignInPageProps = {
  searchParams: Promise<{ mode?: string; next?: string }>;
};

export default async function SignInPage({ searchParams }: SignInPageProps) {
  const user = await getSessionUser();
  if (user) redirect(hasMerchantRole(user) ? "/merchant" : "/account");
  const params = await searchParams;
  const initialMode = params.mode === "register" ? "register" : "login";
  const nextPath = params.next?.startsWith("/") && !params.next.startsWith("//")
    ? params.next
    : null;

  return (
    <main className={styles.authPage}>
      <Link href="/" className={styles.authBrand}>
        Ember &amp; Leaf
      </Link>
      <AuthForm initialMode={initialMode} nextPath={nextPath} />
      <p className={styles.authFootnote}>Secure access for customers and merchant operators.</p>
    </main>
  );
}
