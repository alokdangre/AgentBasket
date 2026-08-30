"use client";

import Image from "next/image";
import Link from "next/link";
import { useState } from "react";

import {
  apiErrorMessage,
  type ApiError,
  type Cart,
} from "@/lib/account-types";
import { formatMoney, productImages } from "@/lib/storefront-data";
import styles from "@/styles/account.module.css";

export function CartView({ initialCart }: { initialCart: Cart }) {
  const [cart, setCart] = useState(initialCart);
  const [pendingItem, setPendingItem] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function mutate(itemId: string, method: "PATCH" | "DELETE", quantity?: number) {
    setPendingItem(itemId);
    setMessage(null);
    try {
      const response = await fetch(`/api/commerce/cart/items/${itemId}`, {
        method,
        headers: method === "PATCH" ? { "Content-Type": "application/json" } : undefined,
        body: method === "PATCH" ? JSON.stringify({ quantity }) : undefined,
      });
      const result = (await response.json()) as Cart & ApiError;
      if (!response.ok) {
        setMessage(apiErrorMessage(result, "Could not update your cart."));
        return;
      }
      setCart(result);
      window.dispatchEvent(new Event("cart:updated"));
    } catch {
      setMessage("The service is unavailable. Try again shortly.");
    } finally {
      setPendingItem(null);
    }
  }

  if (!cart.items.length) {
    return (
      <section className={styles.emptyCart}>
        <h1>Your cart is ready for a first cup.</h1>
        <p>Choose a café drink, freshly roasted coffee or remarkable tea.</p>
        <Link href="/shop" className={styles.primaryAction}>
          Shop the collection
        </Link>
      </section>
    );
  }

  return (
    <div className={styles.cartLayout}>
      <section className={styles.cartItems}>
        <header>
          <h1>Your cart</h1>
          <span>{cart.item_count} items</span>
        </header>
        {cart.items.map((item) => (
          <article key={item.id} className={styles.cartItem}>
            <div className={styles.cartImage}>
              <Image
                src={productImages[item.product_slug] ?? "/images/citrus-bloom.webp"}
                alt={item.product_name}
                fill
                sizes="112px"
              />
            </div>
            <div className={styles.cartItemCopy}>
              <h2>{item.product_name}</h2>
              <p>{item.variant_name}</p>
              {item.modifiers.length ? (
                <small>{item.modifiers.map((modifier) => modifier.name).join(" · ")}</small>
              ) : null}
              <strong>{formatMoney(item.line_total_minor, cart.currency)}</strong>
            </div>
            <div className={styles.quantityControl}>
              <button
                type="button"
                aria-label={`Decrease ${item.product_name} quantity`}
                disabled={pendingItem === item.id || item.quantity <= 1}
                onClick={() => mutate(item.id, "PATCH", item.quantity - 1)}
              >
                −
              </button>
              <span>{item.quantity}</span>
              <button
                type="button"
                aria-label={`Increase ${item.product_name} quantity`}
                disabled={
                  pendingItem === item.id ||
                  item.quantity >= 25 ||
                  (item.available_quantity !== null && item.quantity >= item.available_quantity)
                }
                onClick={() => mutate(item.id, "PATCH", item.quantity + 1)}
              >
                +
              </button>
              <button
                type="button"
                className={styles.removeAction}
                disabled={pendingItem === item.id}
                onClick={() => mutate(item.id, "DELETE")}
              >
                Remove
              </button>
            </div>
          </article>
        ))}
        {message ? <p className={styles.formError}>{message}</p> : null}
      </section>
      <aside className={styles.cartSummary}>
        <h2>Order summary</h2>
        <dl>
          <div>
            <dt>Subtotal</dt>
            <dd>{formatMoney(cart.subtotal_minor, cart.currency)}</dd>
          </div>
          <div>
            <dt>Delivery</dt>
            <dd>Calculated at checkout</dd>
          </div>
        </dl>
        <div className={styles.cartTotal}>
          <span>Total before delivery</span>
          <strong>{formatMoney(cart.subtotal_minor, cart.currency)}</strong>
        </div>
        <Link href="/checkout/review" className={styles.primaryAction}>
          Review checkout
        </Link>
        <Link href="/shop" className={styles.secondaryAction}>
          Continue shopping
        </Link>
      </aside>
    </div>
  );
}
