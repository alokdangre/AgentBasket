"use client";

import { useMemo, useRef, useState } from "react";

import {
  apiErrorMessage,
  type Address,
  type ApiError,
  type PaymentInstrument,
  type PurchaseIntentStatus,
  type RazorpayRecurringAuthorizationResult,
  type RazorpayRecurringAuthorizationSession,
  type ScheduledPurchase,
  type ScheduledPurchaseAuthorization,
  type ScheduledPurchaseChallenge,
  type ScheduledPurchaseList,
} from "@/lib/account-types";
import { loadRazorpayCheckout, type RazorpaySuccess } from "@/lib/razorpay-checkout";
import { formatMoney } from "@/lib/storefront-data";
import { authenticatePasskey } from "@/lib/webauthn";
import styles from "@/styles/account.module.css";

type AuthorizationReview = {
  challenge: ScheduledPurchaseChallenge;
  approvalKey: string;
};

type ScheduleAction = "pause" | "resume" | "revoke" | "run-now";

const STATUS_LABELS: Record<PurchaseIntentStatus, string> = {
  draft: "Awaiting your approval",
  pending_provider_authorization: "Payment setup pending",
  active: "Autonomous runs active",
  paused: "Paused",
  needs_attention: "Needs your attention",
  completed: "Completed",
  expired: "Expired",
  revoked: "Revoked",
};

const RUN_LABELS: Record<string, string> = {
  pending: "Queued",
  claimed: "Checking terms",
  checkout_created: "Checkout created",
  notification_pending: "Payment confirmation pending",
  payment_pending: "Payment pending",
  succeeded: "Purchased",
  requires_human_action: "Approval needed",
  failed: "Failed safely",
  skipped: "Skipped",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function readableItems(display: Record<string, unknown> | null) {
  if (!display) return [];
  const candidate = Array.isArray(display.items)
    ? display.items
    : Array.isArray(display.lines)
      ? display.lines
      : [];

  return candidate.flatMap((value) => {
    if (!isRecord(value)) return [];
    const acceptable = Array.isArray(value.acceptable_items)
      ? value.acceptable_items
      : Array.isArray(value.acceptable_variants)
        ? value.acceptable_variants
        : [];
    const alternatives = acceptable.flatMap((item) => {
      if (!isRecord(item)) return [];
      const label = item.title ?? item.name;
      return typeof label === "string" ? [label] : [];
    });
    const directTitle = value.title ?? value.product_name ?? value.name;
    const title =
      alternatives.length > 0
        ? alternatives.join(" or ")
        : typeof directTitle === "string"
          ? directTitle
          : "Approved catalog item";
    return [{ title, quantity: typeof value.quantity === "number" ? value.quantity : 1 }];
  });
}

function formatDate(value: string | null, timeZone: string): string {
  if (!value) return "No further run";
  try {
    return new Intl.DateTimeFormat("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone,
    }).format(new Date(value));
  } catch {
    return new Date(value).toLocaleString("en-IN");
  }
}

function displayText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function exactDestination(display: Record<string, unknown> | null, fallback: string): string {
  if (!display || !isRecord(display.fulfillment)) return fallback;
  const address = display.fulfillment.address;
  if (!isRecord(address)) return fallback;
  const parts = [address.line_one, address.line_two, address.city, address.postal_code]
    .map(displayText)
    .filter((value): value is string => value !== null);
  return parts.length > 0 ? parts.join(", ") : fallback;
}

function exactInstrument(display: Record<string, unknown> | null, fallback: string): string {
  if (!display || !isRecord(display.payment)) return fallback;
  return displayText(display.payment.instrument_alias) ?? fallback;
}

function firstExecution(display: Record<string, unknown> | null): string | null {
  if (!display || !isRecord(display.schedule)) return null;
  return displayText(display.schedule.first_run_at);
}

function cadenceLabel(schedule: ScheduledPurchase): string {
  if (schedule.frequency === "once") return "One scheduled purchase";
  const frequency = {
    daily: "day",
    weekly: "week",
    monthly: "month",
  }[schedule.frequency];
  return schedule.interval_count === 1
    ? `Every ${frequency}`
    : `Every ${schedule.interval_count} ${frequency}s`;
}

function TermsSummary({
  schedule,
  display,
  addressLabel,
}: {
  schedule: ScheduledPurchase;
  display: Record<string, unknown> | null;
  addressLabel: string;
}) {
  const items = readableItems(display);
  const destination = exactDestination(display, addressLabel);
  const paymentInstrument = exactInstrument(display, schedule.payment_instrument_alias);
  const nextExecution = schedule.next_execution_at ?? firstExecution(display);

  return (
    <div className={styles.scheduleTerms}>
      {items.length > 0 ? (
        <div className={styles.scheduleItems}>
          <span>Allowed items</span>
          {items.map((item, index) => (
            <strong key={`${item.title}-${index}`}>
              {item.quantity} × {item.title}
            </strong>
          ))}
        </div>
      ) : null}
      <dl>
        <div>
          <dt>Cadence</dt>
          <dd>{cadenceLabel(schedule)}</dd>
        </div>
        <div>
          <dt>Next run</dt>
          <dd>{formatDate(nextExecution, schedule.timezone)}</dd>
        </div>
        <div>
          <dt>Per-order limit</dt>
          <dd>{formatMoney(schedule.max_amount_minor, schedule.currency)}</dd>
        </div>
        <div>
          <dt>Total authorization</dt>
          <dd>{formatMoney(schedule.max_total_minor, schedule.currency)}</dd>
        </div>
        <div>
          <dt>Occurrences</dt>
          <dd>
            {schedule.successful_occurrences} of {schedule.max_occurrences} used
          </dd>
        </div>
        <div>
          <dt>Deliver to</dt>
          <dd>{destination}</dd>
        </div>
        <div>
          <dt>Payment boundary</dt>
          <dd>{paymentInstrument}</dd>
        </div>
        <div>
          <dt>Authorization expires</dt>
          <dd>{formatDate(schedule.expires_at, schedule.timezone)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function ScheduledPurchaseManager({
  initialSchedules,
  addresses,
  paymentInstruments,
}: {
  initialSchedules: ScheduledPurchase[];
  addresses: Address[];
  paymentInstruments: PaymentInstrument[];
}) {
  const [schedules, setSchedules] = useState(initialSchedules);
  const [reviews, setReviews] = useState<Record<string, AuthorizationReview>>({});
  const [runConfirmationId, setRunConfirmationId] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const challengeKeys = useRef(new Map<string, string>());
  const actionKeys = useRef(new Map<string, string>());

  const addressLabels = useMemo(
    () => new Map(addresses.map((address) => [address.id, `${address.label} · ${address.city}`])),
    [addresses],
  );
  const instrumentLabels = useMemo(
    () => new Map(paymentInstruments.map((instrument) => [instrument.id, instrument.alias])),
    [paymentInstruments],
  );
  const instrumentsById = useMemo(
    () => new Map(paymentInstruments.map((instrument) => [instrument.id, instrument])),
    [paymentInstruments],
  );

  async function refreshSchedules() {
    const response = await fetch("/api/commerce/scheduled-purchases", { cache: "no-store" });
    const payload = (await response.json()) as ScheduledPurchaseList & ApiError;
    if (!response.ok || !("scheduled_purchases" in payload)) {
      throw new Error(apiErrorMessage(payload, "Could not refresh scheduled purchases."));
    }
    setSchedules(payload.scheduled_purchases);
  }

  async function reviewAuthorization(schedule: ScheduledPurchase) {
    const key = `challenge:${schedule.id}`;
    const idempotencyKey = challengeKeys.current.get(schedule.id) ?? crypto.randomUUID();
    challengeKeys.current.set(schedule.id, idempotencyKey);
    setBusyKey(key);
    setMessage(null);
    try {
      const response = await fetch(
        `/api/commerce/scheduled-purchases/${schedule.id}/authorization/challenge`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
        },
      );
      const challenge = (await response.json()) as ScheduledPurchaseChallenge & ApiError;
      if (!response.ok || !("webauthn_options" in challenge)) {
        setMessage(apiErrorMessage(challenge, "The authorization review could not start."));
        return;
      }
      setReviews((current) => ({
        ...current,
        [schedule.id]: { challenge, approvalKey: crypto.randomUUID() },
      }));
      challengeKeys.current.delete(schedule.id);
      setMessage("Review every bound below. Your passkey will approve this policy once.");
    } catch {
      setMessage("The Trusted Surface could not load the exact schedule. Nothing was approved.");
    } finally {
      setBusyKey(null);
    }
  }

  async function approveAuthorization(schedule: ScheduledPurchase, review: AuthorizationReview) {
    const key = `approve:${schedule.id}`;
    setBusyKey(key);
    setMessage(null);
    try {
      const webauthnCredential = await authenticatePasskey(
        review.challenge.webauthn_options,
      );
      const response = await fetch(
        `/api/commerce/scheduled-purchases/${schedule.id}/authorization/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": review.approvalKey,
          },
          body: JSON.stringify({
            nonce: review.challenge.nonce,
            display_sha256: review.challenge.display_sha256,
            webauthn_credential: webauthnCredential,
          }),
        },
      );
      const payload = (await response.json()) as ScheduledPurchaseAuthorization & ApiError;
      if (!response.ok || !("scheduled_purchase" in payload)) {
        setMessage(apiErrorMessage(payload, "The schedule authorization was rejected."));
        return;
      }
      setSchedules((current) =>
        current.map((item) =>
          item.id === payload.scheduled_purchase.id ? payload.scheduled_purchase : item,
        ),
      );
      setReviews((current) => {
        const next = { ...current };
        delete next[schedule.id];
        return next;
      });
      setMessage(
        payload.scheduled_purchase.provider_ready
          ? "The bounded AP2 delegation is active. Future runs can execute without another prompt."
          : "The AP2 bounds are signed. Provider recurring authorization is still required before any unattended payment.",
      );
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : "Passkey authorization was interrupted. No schedule was activated.",
      );
    } finally {
      setBusyKey(null);
    }
  }

  async function applyAction(schedule: ScheduledPurchase, action: ScheduleAction) {
    const key = `${action}:${schedule.id}`;
    const idempotencyKey = actionKeys.current.get(key) ?? crypto.randomUUID();
    actionKeys.current.set(key, idempotencyKey);
    setBusyKey(key);
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/scheduled-purchases/${schedule.id}/${action}`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
      });
      const payload = (await response.json()) as ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(payload, `Could not ${action.replace("-", " ")} schedule.`));
        return;
      }
      await refreshSchedules();
      actionKeys.current.delete(key);
      setRunConfirmationId(null);
      setMessage(
        action === "run-now"
          ? "The bounded worker was asked to run now. Refreshing this list will show its verified result."
          : `Schedule ${action === "resume" ? "resumed" : `${action}d`} safely.`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The schedule action was interrupted.");
    } finally {
      setBusyKey(null);
    }
  }

  async function setupRecurringAuthorization(schedule: ScheduledPurchase) {
    const key = `provider:${schedule.id}`;
    const idempotencyKey = actionKeys.current.get(key) ?? crypto.randomUUID();
    actionKeys.current.set(key, idempotencyKey);
    setBusyKey(key);
    setMessage(null);
    try {
      const response = await fetch(
        `/api/commerce/credential-provider/razorpay-upi-autopay/${schedule.id}/session`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
        },
      );
      const session = (await response.json()) as RazorpayRecurringAuthorizationSession & ApiError;
      if (!response.ok || !("provider_order_id" in session)) {
        setMessage(apiErrorMessage(session, "Could not start UPI Autopay authorization."));
        return;
      }
      await loadRazorpayCheckout();
      const Razorpay = window.Razorpay;
      if (!Razorpay) throw new Error("Razorpay Checkout is unavailable.");
      const result = await new Promise<RazorpaySuccess>((resolve, reject) => {
        let settled = false;
        const finish = (operation: () => void) => {
          if (settled) return;
          settled = true;
          operation();
        };
        const checkout = new Razorpay({
          key: session.key_id,
          amount: session.amount_minor,
          currency: session.currency,
          name: session.merchant_name,
          description: session.description,
          order_id: session.provider_order_id,
          customer_id: session.provider_customer_id,
          recurring: 1,
          method: { upi: true },
          prefill: {
            name: session.customer_name,
            email: session.customer_email,
            contact: session.customer_phone,
          },
          theme: { color: "#284c3f" },
          retry: { enabled: true },
          modal: {
            ondismiss: () =>
              finish(() => reject(new Error("UPI Autopay authorization was closed."))),
          },
          handler: (value) => finish(() => resolve(value)),
        });
        checkout.on?.("payment.failed", (failure) =>
          finish(() =>
            reject(
              new Error(
                failure.error?.description ?? "UPI Autopay authorization was rejected.",
              ),
            ),
          ),
        );
        checkout.open();
      });
      const verifyResponse = await fetch(
        `/api/commerce/credential-provider/razorpay-upi-autopay/${schedule.id}/verify`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(result),
        },
      );
      const verified = (await verifyResponse.json()) as
        | RazorpayRecurringAuthorizationResult
        | ApiError;
      if (!verifyResponse.ok || !("token_confirmation_pending" in verified)) {
        setMessage(
          apiErrorMessage(
            verified as ApiError,
            "Could not verify UPI Autopay authorization.",
          ),
        );
        return;
      }
      actionKeys.current.delete(key);
      await refreshSchedules();
      setMessage(
        "Razorpay accepted the authorization. Autonomous runs remain blocked until the verified token.confirmed webhook arrives.",
      );
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : "UPI Autopay setup was interrupted. No autonomous payment was enabled.",
      );
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div className={styles.scheduleManager}>
      <div className={styles.scheduleBoundary}>
        <strong>One approval, bounded future actions</strong>
        <p>
          Your passkey approves the merchant, items, delivery destination, timing and money limits
          once. Only an active schedule may later buy without asking again; pause or revoke stops
          new runs.
        </p>
      </div>

      {schedules.length > 0 ? (
        <div className={styles.scheduleList}>
          {schedules.map((schedule) => {
            const review = reviews[schedule.id];
            const addressLabel = schedule.address_id
              ? (addressLabels.get(schedule.address_id) ?? "Authorized saved address")
              : "Authorized pickup location";
            const instrumentLabel =
              instrumentLabels.get(schedule.payment_instrument_id) ??
              schedule.payment_instrument_alias;
            const paymentInstrument = instrumentsById.get(schedule.payment_instrument_id);
            const isBusy = busyKey?.endsWith(schedule.id) ?? false;
            const canResume = ["paused", "needs_attention"].includes(schedule.status);
            const canRevoke = !["completed", "expired", "revoked"].includes(schedule.status);

            return (
              <article className={styles.scheduleCard} key={schedule.id}>
                <header className={styles.scheduleCardHeader}>
                  <div>
                    <span>Scheduled purchase</span>
                    <h3>{cadenceLabel(schedule)}</h3>
                  </div>
                  <strong data-status={schedule.status}>{STATUS_LABELS[schedule.status]}</strong>
                </header>

                <TermsSummary
                  schedule={{ ...schedule, payment_instrument_alias: instrumentLabel }}
                  display={review?.challenge.display ?? schedule.display}
                  addressLabel={addressLabel}
                />

                {schedule.last_failure_message ? (
                  <p className={styles.scheduleFailure}>
                    {schedule.last_failure_message} No out-of-bounds payment was made.
                  </p>
                ) : null}

                {schedule.status === "pending_provider_authorization" ? (
                  <div className={styles.scheduleApproval}>
                    <p>
                      The AP2 delegation is signed, but Razorpay has not confirmed a recurring
                      payment mandate. No autonomous run can start yet.
                    </p>
                    {paymentInstrument?.instrument_type === "com.razorpay.upi.autopay" ? (
                      <button
                        type="button"
                        className={styles.primaryAction}
                        disabled={isBusy}
                        onClick={() => void setupRecurringAuthorization(schedule)}
                      >
                        {busyKey === `provider:${schedule.id}`
                          ? "Opening UPI authorization…"
                          : "Authorize UPI Autopay once"}
                      </button>
                    ) : (
                      <p className={styles.scheduleFailure}>
                        This draft selected a one-time Checkout method. Recreate it with Razorpay
                        UPI Autopay so the provider can issue a bounded recurring token.
                      </p>
                    )}
                  </div>
                ) : null}

                {review ? (
                  <section className={styles.scheduleApproval}>
                    <div>
                      <strong>Terms your passkey will approve</strong>
                      <span>
                        Display proof {review.challenge.display_sha256.slice(0, 16)}… · expires{" "}
                        {formatDate(review.challenge.expires_at, schedule.timezone)}
                      </span>
                    </div>
                    <details className={styles.schedulePayload}>
                      <summary>Inspect complete signed policy payload</summary>
                      <pre>{JSON.stringify(review.challenge.display, null, 2)}</pre>
                    </details>
                    <p>
                      This approval creates open AP2 Checkout and Payment Mandates. It does not
                      place an order or charge the payment method now.
                    </p>
                    <div className={styles.scheduleActions}>
                      <button
                        type="button"
                        className={styles.primaryAction}
                        disabled={isBusy}
                        onClick={() => void approveAuthorization(schedule, review)}
                      >
                        {busyKey === `approve:${schedule.id}`
                          ? "Waiting for passkey…"
                          : "Approve bounds with passkey"}
                      </button>
                      <button
                        type="button"
                        className={styles.secondaryAction}
                        disabled={isBusy}
                        onClick={() =>
                          setReviews((current) => {
                            const next = { ...current };
                            delete next[schedule.id];
                            return next;
                          })
                        }
                      >
                        Cancel review
                      </button>
                    </div>
                  </section>
                ) : null}

                <div className={styles.scheduleActions}>
                  {schedule.status === "draft" ? (
                    <button
                      type="button"
                      className={styles.primaryAction}
                      disabled={isBusy}
                      onClick={() => void reviewAuthorization(schedule)}
                    >
                      {busyKey === `challenge:${schedule.id}`
                        ? "Loading exact terms…"
                        : "Review one-time authorization"}
                    </button>
                  ) : null}
                  {schedule.status === "active" ? (
                    <>
                      <button
                        type="button"
                        className={styles.secondaryAction}
                        disabled={isBusy}
                        onClick={() => void applyAction(schedule, "pause")}
                      >
                        Pause
                      </button>
                      {runConfirmationId === schedule.id ? (
                        <>
                          <button
                            type="button"
                            className={styles.primaryAction}
                            disabled={isBusy || !schedule.provider_ready}
                            onClick={() => void applyAction(schedule, "run-now")}
                          >
                            Confirm bounded run now
                          </button>
                          <button
                            type="button"
                            className={styles.secondaryAction}
                            disabled={isBusy}
                            onClick={() => setRunConfirmationId(null)}
                          >
                            Cancel
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          className={styles.secondaryAction}
                          disabled={isBusy || !schedule.provider_ready}
                          onClick={() => setRunConfirmationId(schedule.id)}
                        >
                          Run within bounds now
                        </button>
                      )}
                    </>
                  ) : null}
                  {canResume ? (
                    <button
                      type="button"
                      className={styles.primaryAction}
                      disabled={isBusy}
                      onClick={() => void applyAction(schedule, "resume")}
                    >
                      Resume bounded runs
                    </button>
                  ) : null}
                  {canRevoke && schedule.status !== "draft" ? (
                    <button
                      type="button"
                      className={styles.scheduleRevoke}
                      disabled={isBusy}
                      onClick={() => void applyAction(schedule, "revoke")}
                    >
                      Revoke authorization
                    </button>
                  ) : null}
                </div>

                {schedule.authorization_reference ? (
                  <details className={styles.scheduleEvidence}>
                    <summary>AP2 authorization evidence</summary>
                    <div>
                      <span>Open Checkout Mandate</span>
                      <code>{schedule.open_checkout_hash ?? "Unavailable"}</code>
                    </div>
                    <div>
                      <span>Authorization reference</span>
                      <code>{schedule.authorization_reference}</code>
                    </div>
                  </details>
                ) : null}

                {schedule.runs.length > 0 ? (
                  <details className={styles.scheduleRuns}>
                    <summary>Recent autonomous runs ({schedule.runs.length})</summary>
                    <ol>
                      {schedule.runs.slice(0, 5).map((run) => (
                        <li key={run.id} data-status={run.status}>
                          <div>
                            <strong>{RUN_LABELS[run.status] ?? run.status}</strong>
                            <time dateTime={run.scheduled_for}>
                              {formatDate(run.scheduled_for, schedule.timezone)}
                            </time>
                            {run.failure_message ? <small>{run.failure_message}</small> : null}
                          </div>
                          <span>
                            {run.amount_minor > 0
                              ? formatMoney(run.amount_minor, run.currency)
                              : "No charge"}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </details>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <p className={styles.emptyCopy}>
          No scheduled purchases yet. Ask Ember to draft one, then return here to review and
          authorize its exact limits.
        </p>
      )}

      {message ? (
        <p className={styles.formMessage} aria-live="polite">
          {message}
        </p>
      ) : null}
    </div>
  );
}
