"use client";

import { type FormEvent, useState } from "react";

import { apiErrorMessage, type Address, type ApiError } from "@/lib/account-types";
import styles from "@/styles/account.module.css";

type AddressList = { addresses: Address[] };

export function AddressManager({ initialAddresses }: { initialAddresses: Address[] }) {
  const [addresses, setAddresses] = useState(initialAddresses);
  const [adding, setAdding] = useState(initialAddresses.length === 0);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function reload() {
    const response = await fetch("/api/commerce/me/addresses", { cache: "no-store" });
    if (response.ok) {
      const result = (await response.json()) as AddressList;
      setAddresses(result.addresses);
    }
  }

  async function addAddress(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setPending(true);
    setMessage(null);
    const form = new FormData(formElement);
    const payload = {
      label: form.get("label"),
      recipient_name: form.get("recipient_name"),
      phone: form.get("phone"),
      line_one: form.get("line_one"),
      line_two: form.get("line_two") || null,
      landmark: form.get("landmark") || null,
      city: form.get("city"),
      region: form.get("region"),
      postal_code: form.get("postal_code"),
      country_code: "IN",
      is_default: form.get("is_default") === "on",
    };
    try {
      const response = await fetch("/api/commerce/me/addresses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = (await response.json()) as Address & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not save this address."));
        return;
      }
      await reload();
      formElement.reset();
      setAdding(false);
      setMessage("Address saved.");
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  async function makeDefault(id: string) {
    setPending(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/me/addresses/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_default: true }),
      });
      if (response.ok) {
        await reload();
        setMessage("Default address updated.");
      } else {
        setMessage("Could not update the default address.");
      }
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  async function remove(id: string) {
    setPending(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/me/addresses/${id}`, { method: "DELETE" });
      if (response.ok) {
        await reload();
        setMessage("Address removed.");
      } else {
        setMessage("Could not remove this address.");
      }
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.addressManager}>
      <div className={styles.addressList}>
        {addresses.length ? (
          addresses.map((address) => (
            <article key={address.id} className={styles.addressRow}>
              <div className={styles.defaultDot} data-selected={address.is_default} />
              <div>
                <h3>{address.label}</h3>
                {address.is_default ? <strong>Default</strong> : null}
                <p>
                  {address.recipient_name}
                  <br />
                  {address.line_one}
                  {address.line_two ? `, ${address.line_two}` : ""}
                  <br />
                  {address.city}, {address.region} {address.postal_code}
                  <br />
                  {address.phone}
                </p>
              </div>
              <div className={styles.addressActions}>
                {!address.is_default ? (
                  <button type="button" disabled={pending} onClick={() => makeDefault(address.id)}>
                    Make default
                  </button>
                ) : null}
                <button type="button" disabled={pending} onClick={() => remove(address.id)}>
                  Remove
                </button>
              </div>
            </article>
          ))
        ) : (
          <p className={styles.emptyCopy}>No saved addresses yet.</p>
        )}
      </div>
      <button type="button" className={styles.secondaryAction} onClick={() => setAdding(!adding)}>
        {adding ? "Cancel" : "+ Add new address"}
      </button>
      {adding ? (
        <form className={styles.addressForm} onSubmit={addAddress}>
          <div className={styles.formRow}>
            <label>
              Label
              <input name="label" placeholder="Home" required />
            </label>
            <label>
              Full name
              <input name="recipient_name" autoComplete="name" required />
            </label>
          </div>
          <div className={styles.formRow}>
            <label>
              Address line 1
              <input name="line_one" autoComplete="address-line1" required />
            </label>
            <label>
              Address line 2
              <input name="line_two" autoComplete="address-line2" />
            </label>
          </div>
          <div className={styles.formRowThree}>
            <label>
              City
              <input name="city" autoComplete="address-level2" defaultValue="Bengaluru" required />
            </label>
            <label>
              State
              <input
                name="region"
                autoComplete="address-level1"
                defaultValue="Karnataka"
                required
              />
            </label>
            <label>
              PIN code
              <input
                name="postal_code"
                inputMode="numeric"
                pattern="[0-9]{6}"
                autoComplete="postal-code"
                required
              />
            </label>
          </div>
          <div className={styles.formRow}>
            <label>
              Phone number
              <input name="phone" autoComplete="tel" minLength={7} required />
            </label>
            <label>
              Landmark
              <input name="landmark" />
            </label>
          </div>
          <label className={styles.checkboxLabel}>
            <input name="is_default" type="checkbox" />
            Set as default address
          </label>
          <button type="submit" className={styles.primaryAction} disabled={pending}>
            {pending ? "Saving…" : "Save address"}
          </button>
        </form>
      ) : null}
      {message ? <p className={styles.formMessage}>{message}</p> : null}
    </div>
  );
}
