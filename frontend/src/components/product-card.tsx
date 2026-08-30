import Image from "next/image";
import Link from "next/link";

import { ArrowRightIcon } from "@/components/icons";
import {
  type CatalogProduct,
  formatMoney,
  productDisplayNames,
  productImages,
} from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

type ProductCardProps = {
  product: CatalogProduct;
  layout?: "rail" | "grid";
};

export function ProductCard({ product, layout = "rail" }: ProductCardProps) {
  const variant = product.variants[0];
  if (!variant) {
    return null;
  }
  const displayName = productDisplayNames[product.slug] ?? product.name;
  const image = productImages[product.slug] ?? "/images/citrus-bloom.webp";
  const price = formatMoney(variant.price_minor, variant.currency);

  return (
    <article
      id={product.slug}
      className={layout === "grid" ? styles.productGridCard : styles.productCard}
    >
      <div className={styles.productImage}>
        <Image
          src={image}
          alt={product.name}
          fill
          sizes={
            layout === "grid"
              ? "(max-width: 720px) 80vw, 28vw"
              : "(max-width: 720px) 48vw, 12vw"
          }
        />
      </div>
      <div className={styles.productDetails}>
        <div>
          <h3>{displayName}</h3>
          {layout === "grid" ? <p>{product.description}</p> : null}
          <span>from {price}</span>
        </div>
        <Link
          href={`/shop?product=${product.slug}`}
          aria-label={`View ${product.name}`}
          className={styles.productArrow}
        >
          <ArrowRightIcon />
        </Link>
      </div>
    </article>
  );
}
