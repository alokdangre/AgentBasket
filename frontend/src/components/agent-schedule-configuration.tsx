"use client";

import { type FormEvent, useMemo, useState } from "react";

import type { AgentScheduleConfiguration } from "@/lib/account-types";
import styles from "@/styles/storefront.module.css";

type AgentScheduleConfigurationProps = {
  configuration: AgentScheduleConfiguration;
  disabled: boolean;
  active: boolean;
  onSubmit: (message: string) => void;
};

const FREQUENCY_DETAIL = {
  once: "One run",
  daily: "Every day",
  weekly: "Every week",
  monthly: "Every month",
} as const;

function localDateTimeValue(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function positiveNumber(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export function AgentScheduleConfiguration({
  configuration,
  disabled,
  active,
  onSubmit,
}: AgentScheduleConfigurationProps) {
  const [frequency, setFrequency] = useState(configuration.frequency ?? "");
  const [intervalCount, setIntervalCount] = useState("1");
  const [firstRunAt, setFirstRunAt] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [maxOccurrences, setMaxOccurrences] = useState(
    configuration.frequency === "once" ? "1" : "",
  );
  const [maxAmount, setMaxAmount] = useState("");
  const [maxTotal, setMaxTotal] = useState("");
  const interactionDisabled = disabled || !active || !configuration.draft_available;
  const earliestFirstRun = useMemo(
    () => localDateTimeValue(configuration.earliest_first_run_at),
    [configuration.earliest_first_run_at],
  );

  const interval = positiveNumber(intervalCount);
  const occurrences = positiveNumber(maxOccurrences);
  const perOrder = positiveNumber(maxAmount);
  const total = positiveNumber(maxTotal);
  const firstRun = firstRunAt ? new Date(firstRunAt) : null;
  const expiry = expiresAt ? new Date(expiresAt) : null;
  const datesValid = Boolean(
    firstRun &&
      expiry &&
      !Number.isNaN(firstRun.getTime()) &&
      !Number.isNaN(expiry.getTime()) &&
      expiry > firstRun,
  );
  const budgetsValid = Boolean(perOrder && total && total >= perOrder);
  const selectionComplete = Boolean(
    frequency && interval && occurrences && datesValid && budgetsValid,
  );

  function selectFrequency(value: string) {
    setFrequency(value);
    if (value === "once") {
      setIntervalCount("1");
      setMaxOccurrences("1");
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      interactionDisabled ||
      !selectionComplete ||
      !firstRun ||
      !expiry ||
      !interval ||
      !occurrences ||
      !perOrder ||
      !total
    ) {
      return;
    }
    onSubmit(
      `Schedule this purchase ${frequency}, every ${interval} interval, first run at ${firstRun.toISOString()}, expires at ${expiry.toISOString()}, maximum ${occurrences} occurrences, per-order cap INR ${perOrder}, total cap INR ${total}.`,
    );
  }

  if (!active) {
    return (
      <div
        className={`${styles.agentConfiguration} ${styles.agentScheduleConfiguration}`}
        data-superseded="true"
      >
        <header>
          <div>
            <span>Set safe bounds</span>
            <strong>Recurring purchase</strong>
          </div>
          <small>Superseded</small>
        </header>
        <p>This schedule request has been superseded.</p>
      </div>
    );
  }

  const status = !configuration.draft_available
    ? (configuration.availability_message ?? "Recurring scheduling is not available.")
    : !frequency
      ? "Choose a recurrence."
      : !datesValid
        ? "Choose a first run and a later expiry."
        : !occurrences
          ? "Set the maximum number of runs."
          : !budgetsValid
            ? "Set both caps; total must cover at least one order."
            : "Ready to continue the schedule draft.";

  return (
    <form
      className={`${styles.agentConfiguration} ${styles.agentScheduleConfiguration}`}
      onSubmit={submit}
    >
      <header>
        <div>
          <span>Set safe bounds</span>
          <strong>Recurring purchase</strong>
        </div>
        <small>{configuration.draft_available ? "Draft only" : "Unavailable"}</small>
      </header>

      {!configuration.draft_available ? (
        <p className={styles.agentScheduleAvailability} role="status">
          {configuration.availability_message}
        </p>
      ) : null}

      <fieldset disabled={interactionDisabled}>
        <legend>
          Recurrence
          <small>Choose one</small>
        </legend>
        <div className={styles.agentChoiceGrid}>
          {configuration.frequency_options.map((option) => {
            const inputId = `agent-schedule-frequency-${option}`;
            return (
              <label key={option} htmlFor={inputId}>
                <input
                  id={inputId}
                  name="schedule-frequency"
                  type="radio"
                  value={option}
                  checked={frequency === option}
                  required
                  onChange={() => selectFrequency(option)}
                />
                <span>
                  <strong>{option[0].toUpperCase() + option.slice(1)}</strong>
                  <small>{FREQUENCY_DETAIL[option]}</small>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <div className={styles.agentScheduleFields}>
        <label>
          Every
          <span>
            <input
              type="number"
              min="1"
              max="12"
              step="1"
              inputMode="numeric"
              value={intervalCount}
              disabled={interactionDisabled || frequency === "once"}
              required
              onChange={(event) => setIntervalCount(event.target.value)}
            />
            <small>interval(s)</small>
          </span>
        </label>

        <label>
          Maximum runs
          <input
            type="number"
            min="1"
            max="365"
            step="1"
            inputMode="numeric"
            value={maxOccurrences}
            disabled={interactionDisabled || frequency === "once"}
            required
            onChange={(event) => setMaxOccurrences(event.target.value)}
          />
        </label>

        <label className={styles.agentScheduleWide}>
          First run
          <input
            type="datetime-local"
            min={earliestFirstRun || undefined}
            value={firstRunAt}
            disabled={interactionDisabled}
            required
            onChange={(event) => setFirstRunAt(event.target.value)}
          />
          <small>
            Allow at least {configuration.minimum_notification_lead_hours} hours for provider
            notice.
          </small>
        </label>

        <label className={styles.agentScheduleWide}>
          Authorization expires
          <input
            type="datetime-local"
            min={firstRunAt || earliestFirstRun || undefined}
            value={expiresAt}
            disabled={interactionDisabled}
            required
            onChange={(event) => setExpiresAt(event.target.value)}
          />
        </label>

        <label>
          Per-order cap
          <span>
            <small>₹</small>
            <input
              type="number"
              min="1"
              max="100000"
              step="1"
              inputMode="decimal"
              value={maxAmount}
              disabled={interactionDisabled}
              required
              onChange={(event) => setMaxAmount(event.target.value)}
            />
          </span>
        </label>

        <label>
          Total cap
          <span>
            <small>₹</small>
            <input
              type="number"
              min="1"
              max="1000000"
              step="1"
              inputMode="decimal"
              value={maxTotal}
              disabled={interactionDisabled}
              required
              onChange={(event) => setMaxTotal(event.target.value)}
            />
          </span>
        </label>
      </div>

      <footer>
        <p role="status">{status}</p>
        <span>No schedule or payment is authorized here.</span>
        <button type="submit" disabled={interactionDisabled || !selectionComplete}>
          Continue schedule
        </button>
      </footer>
    </form>
  );
}
