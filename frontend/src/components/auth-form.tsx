"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { apiErrorMessage, type ApiError, type SessionUser } from "@/lib/account-types";
import styles from "@/styles/account.module.css";

type AuthFormProps = {
  initialMode: "login" | "register";
  nextPath: string | null;
};

export function AuthForm({ initialMode, nextPath }: AuthFormProps) {
  const router = useRouter();
  const [mode, setMode] = useState(initialMode);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    const payload =
      mode === "register"
        ? {
            full_name: form.get("full_name"),
            phone: form.get("phone") || null,
            email: form.get("email"),
            password: form.get("password"),
          }
        : { email: form.get("email"), password: form.get("password") };
    try {
      const response = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = (await response.json()) as { user?: SessionUser } & ApiError;
      if (!response.ok || !result.user) {
        setError(apiErrorMessage(result, "Unable to sign in."));
        return;
      }
      const destination =
        nextPath ??
        (result.user.role === "merchant_admin" || result.user.role === "merchant_staff"
          ? "/merchant"
          : "/account");
      router.push(destination);
      router.refresh();
    } catch {
      setError("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.authPanel}>
      <div className={styles.authTabs} role="tablist" aria-label="Account access">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "login"}
          onClick={() => {
            setMode("login");
            setError(null);
          }}
        >
          Sign in
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "register"}
          onClick={() => {
            setMode("register");
            setError(null);
          }}
        >
          Create account
        </button>
      </div>
      <form onSubmit={submit} className={styles.authForm}>
        <div>
          <h1>{mode === "login" ? "Welcome back." : "Create your account."}</h1>
          <p>
            {mode === "login"
              ? "Sign in to manage addresses, your cart and future orders."
              : "Save your preferences and move through checkout with less friction."}
          </p>
        </div>
        {mode === "register" ? (
          <div className={styles.formRow}>
            <label>
              Full name
              <input name="full_name" autoComplete="name" minLength={2} required />
            </label>
            <label>
              Phone number
              <input name="phone" autoComplete="tel" minLength={7} />
            </label>
          </div>
        ) : null}
        <label>
          Email address
          <input name="email" type="email" autoComplete="email" required />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={mode === "register" ? 12 : 1}
            maxLength={128}
            required
          />
          {mode === "register" ? <small>Use at least 12 characters.</small> : null}
        </label>
        {error ? <p className={styles.formError}>{error}</p> : null}
        <button type="submit" className={styles.primaryAction} disabled={pending}>
          {pending ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
      </form>
    </div>
  );
}
