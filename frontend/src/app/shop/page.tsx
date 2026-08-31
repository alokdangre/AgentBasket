import type { Metadata } from "next";

import { ProductCard } from "@/components/product-card";
import { ProductPurchasePanel } from "@/components/product-purchase-panel";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { getCatalog } from "@/lib/commerce-api";
import type { CatalogProduct } from "@/lib/storefront-data";
import styles from "@/styles/storefront.module.css";

export const metadata: Metadata = {
  title: "Shop",
  description: "Shop Ember & Leaf coffee, tea and café-made drinks.",
};

const typeTitles: Partial<Record<CatalogProduct["product_type"], string>> = {
  prepared_beverage: "Café-made today",
  packaged_coffee: "Freshly roasted coffee",
  packaged_tea: "Remarkable tea",
  accessory: "Brewing essentials",
};

type ShopProps = {
  searchParams: Promise<{
    type?: CatalogProduct["product_type"];
    postal_code?: string;
    product?: string;
  }>;
};

export default async function Shop({ searchParams }: ShopProps) {
  const params = await searchParams;
  const postalCode = params.postal_code?.trim() || "560038";
  const { catalog, source } = await getCatalog({ postalCode });
  const products = params.type
    ? catalog.products.filter((product) => product.product_type === params.type)
    : catalog.products;
  const title = (params.type && typeTitles[params.type]) || "The collection";
  const selectedProduct = params.product
    ? catalog.products.find((product) => product.slug === params.product)
    : undefined;

  return (
    <div className={styles.siteShell} data-catalog-source={source}>
      <SiteHeader postalCode={postalCode} />
      <main className={styles.shopPage}>
        <header className={styles.shopHeading}>
          <h1>{title}</h1>
          <p>
            Prepared in Indiranagar, roasted in small batches and selected for
            everyday rituals.
          </p>
        </header>
        {selectedProduct ? <ProductPurchasePanel product={selectedProduct} /> : null}
        <div className={styles.catalogGrid}>
          {products.map((product) => (
            <ProductCard key={product.id} product={product} layout="grid" />
          ))}
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
