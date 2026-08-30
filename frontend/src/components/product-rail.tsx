import Link from "next/link";

import { ArrowRightIcon } from "@/components/icons";
import { ProductCard } from "@/components/product-card";
import type { CatalogProduct } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type ProductRailProps = {
  products: CatalogProduct[];
  title?: string;
};

export function ProductRail({
  products,
  title = "Picked for today",
}: ProductRailProps) {
  return (
    <section id="picked-for-today" className={styles.productsSection}>
      <div className={styles.sectionHeading}>
        <h2>{title}</h2>
        <span className={styles.headingRule} />
        <Link href="/shop">
          <span>See all</span>
          <ArrowRightIcon />
        </Link>
      </div>
      {products.length ? (
        <div className={styles.productRail}>
          {products.map((product) => (
            <ProductCard key={product.id} product={product} />
          ))}
        </div>
      ) : (
        <p className={styles.emptyResults}>
          No products match that search. Try coffee, tea or chai.
        </p>
      )}
    </section>
  );
}
