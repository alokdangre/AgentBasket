import Image from "next/image";
import Link from "next/link";

import styles from "@/styles/storefront.module.css";

export function StorySection() {
  return (
    <section className={styles.story}>
      <div className={styles.storyImage}>
        <Image
          src="/images/roastery.webp"
          alt="Ember & Leaf's matte-black coffee roaster in a sunlit Bengaluru workspace"
          fill
          sizes="(max-width: 720px) 100vw, 42vw"
        />
      </div>
      <div className={styles.storyCopy}>
        <div>
          <h2>Roasted here. Delivered with intent.</h2>
          <p>
            We roast in small batches in Indiranagar, Bengaluru. Thoughtful
            sourcing, careful roasting, and fast local delivery—so every cup
            arrives as it should.
          </p>
        </div>
        <Link href="/shop?type=packaged_coffee" className={styles.primaryButton}>
          Visit the roastery
        </Link>
      </div>
    </section>
  );
}
