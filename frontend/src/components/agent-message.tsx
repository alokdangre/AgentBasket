import Link from "next/link";
import ReactMarkdown from "react-markdown";

import { AgentCheckoutPayment } from "@/components/agent-checkout-payment";
import { AgentProductConfiguration } from "@/components/agent-product-configuration";
import { AgentScheduleConfiguration } from "@/components/agent-schedule-configuration";
import { ScheduledPurchaseArtifact } from "@/components/scheduled-purchase-artifact";
import type { AgentMessage } from "@/lib/account-types";
import { formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type AgentMessageViewProps = {
  message: AgentMessage;
  disabled: boolean;
  configurationActive: boolean;
  onSuggestion: (suggestion: string) => void;
  onPaid: () => void;
};

function normalizedWords(value: string) {
  return value.toLocaleLowerCase("en").match(/[a-z0-9]+/g)?.join(" ") ?? "";
}

function productsNamedInMessage(products: AgentMessage["structured_content"]["products"], text: string) {
  const normalizedText = ` ${normalizedWords(text)} `;
  return (products ?? [])
    .map((product) => ({
      product,
      position: normalizedText.indexOf(` ${normalizedWords(product.name)} `),
    }))
    .filter(({ position }) => position >= 0)
    .sort((left, right) => left.position - right.position)
    .map(({ product }) => product);
}

export function AgentMessageView({
  message,
  disabled,
  configurationActive,
  onSuggestion,
  onPaid,
}: AgentMessageViewProps) {
  const structured = message.structured_content;
  const products = productsNamedInMessage(structured.products, message.content);
  const activity = structured.activity ?? [];
  const verifiedCount = activity.filter((item) => item.status === "success").length;
  const safeFallbackCount = activity.length - verifiedCount;

  return (
    <article
      className={message.role === "user" ? styles.agentUserMessage : styles.agentAssistantMessage}
    >
      <span className={styles.agentMessageAuthor}>
        {message.role === "user" ? "You" : "Ember"}
      </span>
      {message.role === "assistant" ? (
        <div className={styles.agentMessageContent}>
          <ReactMarkdown
            skipHtml
            disallowedElements={["a", "img"]}
            unwrapDisallowed
          >
            {message.content}
          </ReactMarkdown>
        </div>
      ) : (
        <p>{message.content}</p>
      )}

      {structured.product_configuration ? (
        <AgentProductConfiguration
          configuration={structured.product_configuration}
          disabled={disabled}
          active={configurationActive}
          onSubmit={onSuggestion}
        />
      ) : null}

      {structured.schedule_configuration ? (
        <AgentScheduleConfiguration
          configuration={structured.schedule_configuration}
          disabled={disabled}
          active={configurationActive}
          onSubmit={onSuggestion}
        />
      ) : null}

      {products.length ? (
        <div className={styles.agentProducts}>
          {products.map((product, index) => {
            const variant = product.variants.reduce(
              (lowest, candidate) =>
                candidate.price_minor < lowest.price_minor ? candidate : lowest,
              product.variants[0],
            );
            if (!variant) return null;
            return (
              <button
                type="button"
                className={styles.agentProduct}
                key={product.id}
                disabled={disabled}
                aria-label={`Ask Ember to add ${product.name} to the cart`}
                onClick={() => onSuggestion(`Add one ${product.name} to my cart.`)}
              >
                <span className={styles.agentProductIndex} aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className={styles.agentProductCopy}>
                  <strong>{product.name}</strong>
                  <span>{product.recommendation_reason ?? product.description}</span>
                  <small>
                    {product.product_type.replaceAll("_", " ")} · from{" "}
                    {formatMoney(variant.price_minor, variant.currency)}
                  </small>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}

      {structured.cart ? (
        <div className={styles.agentCommerceArtifact}>
          <div>
            <span>Current cart</span>
            <strong>
              {structured.cart.item_count} {structured.cart.item_count === 1 ? "item" : "items"}
            </strong>
          </div>
          <div>
            <span>Subtotal</span>
            <strong>
              {formatMoney(structured.cart.subtotal_minor, structured.cart.currency)}
            </strong>
          </div>
          <Link href="/cart">Review cart</Link>
        </div>
      ) : null}

      {structured.checkout ? (
        <AgentCheckoutPayment
          checkout={structured.checkout}
          disabled={disabled}
          onPaid={onPaid}
        />
      ) : null}

      {structured.scheduled_purchase ? (
        <ScheduledPurchaseArtifact schedule={structured.scheduled_purchase} />
      ) : null}

      {structured.memory ? (
        <div className={styles.agentCommerceArtifact} data-status={structured.memory.status}>
          <div>
            <span>Preference memory</span>
            <strong>
              {structured.memory.status === "saved"
                ? `${structured.memory.kind?.replaceAll("_", " ")} saved`
                : structured.memory.status === "forgotten"
                  ? `${structured.memory.kind?.replaceAll("_", " ")} forgotten`
                  : "No memory changed"}
            </strong>
          </div>
          {structured.memory.value !== undefined ? (
            <span>{String(structured.memory.value)}</span>
          ) : null}
        </div>
      ) : null}

      {structured.suggestions?.length ? (
        <div className={styles.agentSuggestions}>
          {structured.suggestions.map((suggestion) => (
            <button
              type="button"
              key={suggestion}
              disabled={disabled}
              onClick={() => onSuggestion(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}

      {activity.length ? (
        <details className={styles.agentActivity}>
          <summary>
            {safeFallbackCount > 0
              ? `${verifiedCount} verified · ${safeFallbackCount} safe ${
                  safeFallbackCount === 1 ? "fallback" : "fallbacks"
                }`
              : `${activity.length} verified ${activity.length === 1 ? "action" : "actions"}`}
          </summary>
          <ul>
            {activity.map((item, index) => (
              <li key={`${item.tool}-${index}`} data-status={item.status}>
                {item.label}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}
