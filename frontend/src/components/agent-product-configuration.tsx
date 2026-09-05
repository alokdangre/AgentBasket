"use client";

import { type FormEvent, useState } from "react";

import type {
  AgentProductConfiguration as ProductConfiguration,
} from "@/lib/account-types";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type SelectedOptions = Record<string, string[]>;

type AgentProductConfigurationProps = {
  configuration: ProductConfiguration;
  disabled: boolean;
  active: boolean;
  onSubmit: (message: string) => void;
};

export function AgentProductConfiguration({
  configuration,
  disabled,
  active,
  onSubmit,
}: AgentProductConfigurationProps) {
  const [variantId, setVariantId] = useState("");
  const [selectedOptions, setSelectedOptions] = useState<SelectedOptions>({});
  const selectedVariant = configuration.variants.find((variant) => variant.id === variantId);
  const selectedModifierOptions = configuration.modifier_groups.flatMap((group) => {
    const selected = new Set(selectedOptions[group.id] ?? []);
    return group.options.filter((option) => selected.has(option.id));
  });
  const currency = selectedVariant?.currency ?? configuration.variants[0]?.currency ?? "INR";
  const missingChoices = configuration.modifier_groups
    .filter(
      (group) => (selectedOptions[group.id]?.length ?? 0) < group.minimum_selections,
    )
    .map((group) => group.name.toLocaleLowerCase("en"));
  if (!selectedVariant) missingChoices.unshift("size");

  const selectionComplete = missingChoices.length === 0;
  const interactionDisabled = disabled || !active;
  const isSchedule = configuration.purpose === "schedule_draft";
  const selectedTotal = selectedVariant
    ? (selectedVariant.price_minor +
        selectedModifierOptions.reduce(
          (total, option) => total + option.price_delta_minor,
          0,
        )) * configuration.quantity
    : null;

  function selectOne(groupId: string, optionId: string) {
    setSelectedOptions((current) => ({
      ...current,
      [groupId]: optionId ? [optionId] : [],
    }));
  }

  function toggleMultiple(groupId: string, optionId: string, maximum: number) {
    setSelectedOptions((current) => {
      const selected = current[groupId] ?? [];
      const next = selected.includes(optionId)
        ? selected.filter((id) => id !== optionId)
        : selected.length < maximum
          ? [...selected, optionId]
          : selected;
      return { ...current, [groupId]: next };
    });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedVariant || !selectionComplete || interactionDisabled) return;
    const quantity = configuration.quantity === 1 ? "one" : String(configuration.quantity);
    const choices = [
      selectedVariant.name,
      ...selectedModifierOptions.map((option) => option.name),
    ];
    onSubmit(
      isSchedule
        ? `Schedule ${quantity} ${configuration.product_name}, ${choices.join(", ")}.`
        : `Add ${quantity} ${configuration.product_name}, ${choices.join(", ")} to my cart.`,
    );
  }

  if (!active) {
    return (
      <div className={styles.agentConfiguration} data-superseded="true">
        <header>
          <div>
            <span>Configure</span>
            <strong>{configuration.product_name}</strong>
          </div>
          <small>Superseded</small>
        </header>
        <p>This choice request has been superseded.</p>
      </div>
    );
  }

  const status = selectionComplete
    ? isSchedule
      ? "Ready to continue this schedule."
      : "Ready to add this configuration."
    : `Choose ${missingChoices.join(", ")}.`;

  return (
    <form className={styles.agentConfiguration} onSubmit={submit}>
      <header>
        <div>
          <span>Configure</span>
          <strong>{configuration.product_name}</strong>
        </div>
        <small>
          {configuration.quantity} {configuration.quantity === 1 ? "item" : "items"}
        </small>
      </header>

      <fieldset disabled={interactionDisabled}>
        <legend>Size</legend>
        <div className={styles.agentChoiceGrid}>
          {configuration.variants.map((variant) => {
            const inputId = `agent-${configuration.product_id}-variant-${variant.id}`;
            return (
              <label key={variant.id} htmlFor={inputId}>
                <input
                  id={inputId}
                  name={`variant-${configuration.product_id}`}
                  type="radio"
                  value={variant.id}
                  checked={variantId === variant.id}
                  required
                  onChange={() => setVariantId(variant.id)}
                />
                <span>
                  <strong>{variant.name}</strong>
                  <small>
                    {variant.size_label ? `${variant.size_label} · ` : ""}
                    {formatMoney(variant.price_minor, variant.currency)}
                  </small>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      {configuration.modifier_groups.map((group) => {
        const selected = selectedOptions[group.id] ?? [];
        const singleChoice = group.maximum_selections === 1;
        return (
          <fieldset key={group.id} disabled={interactionDisabled}>
            <legend>
              {group.name}
              <small>{group.required ? "Choose one" : "Optional"}</small>
            </legend>
            <div className={styles.agentChoiceGrid}>
              {!group.required && singleChoice ? (
                <label htmlFor={`agent-${configuration.product_id}-${group.id}-none`}>
                  <input
                    id={`agent-${configuration.product_id}-${group.id}-none`}
                    name={`modifier-${configuration.product_id}-${group.id}`}
                    type="radio"
                    value=""
                    checked={selected.length === 0}
                    onChange={() => selectOne(group.id, "")}
                  />
                  <span>
                    <strong>None</strong>
                    <small>No extra charge</small>
                  </span>
                </label>
              ) : null}
              {group.options.map((option) => {
                const inputId = `agent-${configuration.product_id}-${group.id}-${option.id}`;
                const checked = selected.includes(option.id);
                return (
                  <label key={option.id} htmlFor={inputId}>
                    <input
                      id={inputId}
                      name={`modifier-${configuration.product_id}-${group.id}`}
                      type={singleChoice ? "radio" : "checkbox"}
                      value={option.id}
                      checked={checked}
                      required={singleChoice && group.required}
                      disabled={
                        interactionDisabled ||
                        (!singleChoice && !checked && selected.length >= group.maximum_selections)
                      }
                      onChange={() =>
                        singleChoice
                          ? selectOne(group.id, option.id)
                          : toggleMultiple(group.id, option.id, group.maximum_selections)
                      }
                    />
                    <span>
                      <strong>{option.name}</strong>
                      <small>
                        {option.price_delta_minor
                          ? `+${formatMoney(option.price_delta_minor, currency)}`
                          : "Included"}
                      </small>
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        );
      })}

      <footer>
        <p role="status">{status}</p>
        {selectedTotal !== null && selectedVariant ? (
          <span>
            {formatMoney(selectedTotal, selectedVariant.currency)} before delivery
          </span>
        ) : null}
        <button type="submit" disabled={interactionDisabled || !selectionComplete}>
          {isSchedule ? "Use in schedule" : "Add selected item"}
        </button>
      </footer>
    </form>
  );
}
