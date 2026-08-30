"use client";

import { type FormEvent, useState } from "react";

import {
  apiErrorMessage,
  type ApiError,
  type SessionUser,
} from "@/lib/account-types";
import styles from "@/styles/account.module.css";

export function ProfileForm({ user }: { user: SessionUser }) {
  const [profile, setProfile] = useState(user);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setMessage(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/commerce/me", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          full_name: form.get("full_name"),
          phone: form.get("phone"),
        }),
      });
      const result = (await response.json()) as SessionUser & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not save your profile."));
        return;
      }
      setProfile(result);
      setMessage("Profile saved.");
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className={styles.profileForm} onSubmit={submit}>
      <div className={styles.formRow}>
        <label>
          Full name
          <input name="full_name" defaultValue={profile.full_name} minLength={2} required />
        </label>
        <label>
          Email address
          <input value={profile.email} readOnly aria-describedby="email-note" />
          <small id="email-note">Contact support to change your sign-in email.</small>
        </label>
      </div>
      <label>
        Phone number
        <input
          name="phone"
          defaultValue={profile.phone ?? ""}
          autoComplete="tel"
          minLength={7}
          required
        />
      </label>
      <div className={styles.formActions}>
        <button type="submit" className={styles.primaryAction} disabled={pending}>
          {pending ? "Saving…" : "Save changes"}
        </button>
        {message ? <span role="status">{message}</span> : null}
      </div>
    </form>
  );
}
