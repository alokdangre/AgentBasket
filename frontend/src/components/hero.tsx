import Image from "next/image";
import Link from "next/link";

import { DeliveryIcon } from "@/components/icons";
import styles from "@/styles/storefront.module.css";

export function Hero() {
  return (
    <>
      <section className={styles.hero}>
        <div className={styles.heroCopy}>
          <h1>
            Coffee for right now.
            <br />
            Rituals for later.
          </h1>
          <p>
            Café-made drinks when you want them, freshly roasted coffee and
            remarkable tea for the days ahead.
          </p>
          <div className={styles.heroActions}>
            <Link href="/shop" className={styles.primaryButton}>
              Shop the collection
            </Link>
            <Link
              href="/shop?type=prepared_beverage"
              className={styles.secondaryButton}
            >
              Order a café drink
            </Link>
          </div>
        </div>
        <div className={styles.heroMedia}>
          <Image
            className={styles.heroDesktopImage}
            src="/images/hero-still-life.webp"
            alt="Cold brew, Ember & Leaf coffee, tea and a ceramic cup by a sunlit window"
            fill
            priority
            sizes="(max-width: 720px) 1px, 56vw"
          />
          <Image
            className={styles.heroMobileImage}
            src="/images/hero-mobile.webp"
            alt=""
            fill
            priority
            sizes="(max-width: 720px) 100vw, 1px"
          />
        </div>
      </section>
      <div className={styles.deliveryLine}>
        <DeliveryIcon />
        <span>Indiranagar · 45–90 min delivery</span>
      </div>
    </>
  );
}
