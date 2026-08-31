import Image from "next/image";
import Link from "next/link";

import { AgentCheckoutPayment } from "@/components/agent-checkout-payment";
import type { AgentMessage } from "@/lib/account-types";
import { formatMoney, productImages } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type AgentMessageViewProps = {
  message: AgentMessage;
  disabled: boolean;
  onSuggestion: (suggestion: string) => void;
  onPaid: () => void;
};

export function AgentMessageView({
  message,
  disabled,
  onSuggestion,
  onPaid,
}: AgentMessageViewProps) {
  const structured = message.structured_content;
  const products = structured.products ?? [];
  const activity = structured.activity ?? [];

  return (
    <article
      className={message.role === "user" ? styles.agentUserMessage : styles.agentAssistantMessage}
    >
      <span className={styles.agentMessageAuthor}>
        {message.role === "user" ? "You" : "Ember"}
      </span>
      <p>{message.content}</p>

      {products.length ? (
        <div className={styles.agentProducts}>
          {products.map((product) => {
            const variant = product.variants.reduce(
              (lowest, candidate) =>
                candidate.price_minor < lowest.price_minor ? candidate : lowest,
              product.variants[0],
            );
            if (!variant) return null;
            const image = productImages[product.slug] ?? "/images/citrus-bloom.webp";
            return (
              <Link
                href={`/shop?product=${product.slug}`}
                className={styles.agentProduct}
                key={product.id}
              >
                <span className={styles.agentProductImage}>
                  <Image src={image} alt="" fill sizes="72px" />
                </span>
                <span className={styles.agentProductCopy}>
                  <strong>{product.name}</strong>
                  <span>{product.recommendation_reason ?? product.description}</span>
                  <small>from {formatMoney(variant.price_minor, variant.currency)}</small>
                </span>
              </Link>
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
            {activity.length} verified {activity.length === 1 ? "action" : "actions"}
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
