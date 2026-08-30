"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import {
  apiErrorMessage,
  type ApiError,
  type Cart,
} from "@/lib/account-types";
import { type CatalogProduct, formatMoney } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";

export function ProductPurchasePanel({ product }: { product: CatalogProduct }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setMessage(null);
    const form = new FormData(event.currentTarget);
    const modifierIds = product.modifier_groups.flatMap((group) => {
      const value = form.get(`modifier-${group.id}`);
      return typeof value === "string" && value ? [value] : [];
    });
    try {
      const response = await fetch("/api/commerce/cart/items", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          variant_id: form.get("variant_id"),
          quantity: Number(form.get("quantity")),
          modifier_option_ids: modifierIds,
        }),
      });
      const result = (await response.json()) as Cart & ApiError;
      if (response.status === 401) {
        router.push(`/account/sign-in?next=${encodeURIComponent(`/shop?product=${product.slug}`)}`);
        return;
      }
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not add this product."));
        return;
      }
      window.dispatchEvent(new Event("cart:updated"));
      setMessage(`Added to cart. ${result.item_count} item${result.item_count === 1 ? "" : "s"} now.`);
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPending(false);
    }
  }

  return (
    <section className={styles.purchasePanel} aria-labelledby="configure-product">
      <div>
        <h2 id="configure-product">{product.name}</h2>
        <p>{product.description}</p>
      </div>
      <form onSubmit={add}>
        <label>
          Size
          <select name="variant_id" required>
            {product.variants.map((variant) => (
              <option key={variant.id} value={variant.id}>
                {variant.name} · {formatMoney(variant.price_minor, variant.currency)}
              </option>
            ))}
          </select>
        </label>
        {product.modifier_groups.map((group) => (
          <label key={group.id}>
            {group.name}
            <select name={`modifier-${group.id}`} required={group.required}>
              {!group.required ? <option value="">None</option> : null}
              {group.options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                  {option.price_delta_minor
                    ? ` · +${formatMoney(option.price_delta_minor, "INR")}`
                    : ""}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label>
          Quantity
          <input name="quantity" type="number" min={1} max={25} defaultValue={1} required />
        </label>
        <button type="submit" className={styles.primaryAction} disabled={pending}>
          {pending ? "Adding…" : "Add to cart"}
        </button>
      </form>
      {message ? <p className={styles.formMessage}>{message}</p> : null}
    </section>
  );
}
