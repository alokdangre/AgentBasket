"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { BagIcon } from "@/components/icons";
import type { Cart } from "@/lib/account-types";
import styles from "@/styles/storefront.module.css";

export function CartIndicator() {
  const [count, setCount] = useState(0);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const response = await fetch("/api/commerce/cart", { cache: "no-store" });
      if (active && response.ok) {
        const cart = (await response.json()) as Cart;
        setCount(cart.item_count);
      }
    };
    void refresh();
    window.addEventListener("cart:updated", refresh);
    return () => {
      active = false;
      window.removeEventListener("cart:updated", refresh);
    };
  }, []);

  return (
    <Link href="/cart" className={styles.bagButton} aria-label={`Shopping bag, ${count} items`}>
      <BagIcon />
      <span>{count}</span>
    </Link>
  );
}
