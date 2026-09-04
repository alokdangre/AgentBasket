"use client";

import { useState } from "react";

import { apiErrorMessage, type ApiError, type Passkey } from "@/lib/account-types";
import { createPasskey } from "@/lib/webauthn";
import styles from "@/styles/account.module.css";

type RegistrationOptions = {
  ceremony_id: string;
  public_key: PublicKeyCredentialCreationOptionsJSON;
};

export function PasskeyManager({ initialPasskeys }: { initialPasskeys: Passkey[] }) {
  const [passkeys, setPasskeys] = useState(initialPasskeys);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function addPasskey() {
    setPending(true);
    setMessage(null);
    try {
      const optionsResponse = await fetch("/api/commerce/me/passkeys/registration/options", {
        method: "POST",
      });
      const options = (await optionsResponse.json()) as RegistrationOptions & ApiError;
      if (!optionsResponse.ok) {
        setMessage(apiErrorMessage(options, "Passkey setup could not start."));
        return;
      }
      const credential = await createPasskey(options.public_key);
      const verifyResponse = await fetch("/api/commerce/me/passkeys/registration/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ceremony_id: options.ceremony_id,
          credential,
          label: "Purchase approval passkey",
        }),
      });
      const verified = (await verifyResponse.json()) as Passkey & ApiError;
      if (!verifyResponse.ok) {
        setMessage(apiErrorMessage(verified, "Passkey proof could not be verified."));
        return;
      }
      setPasskeys((current) => [...current, verified]);
      setMessage(
        "Passkey added. Immediate purchases and one-time schedule setup now use device approval.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Passkey setup was interrupted.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.passkeyManager}>
      <div className={styles.passkeyList}>
        {passkeys.length ? (
          passkeys.map((passkey) => (
            <div key={passkey.id} className={styles.passkeyRow}>
              <div>
                <strong>{passkey.label}</strong>
                <span>
                  {passkey.device_type.replaceAll("_", " ")}
                  {passkey.backed_up ? " · synced" : " · this device"}
                </span>
              </div>
              <small>{passkey.last_used_at ? "Used for an approval" : "Ready"}</small>
            </div>
          ))
        ) : (
          <p>No passkey yet. Ember cannot authorize a purchase until you add one.</p>
        )}
      </div>
      <button type="button" className={styles.primaryAction} disabled={pending} onClick={addPasskey}>
        {pending ? "Waiting for device…" : "Add purchase passkey"}
      </button>
      {message ? <p className={styles.formMessage}>{message}</p> : null}
    </div>
  );
}
