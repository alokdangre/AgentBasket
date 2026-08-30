import { CategoryRail } from "@/components/category-rail";
import { Hero } from "@/components/hero";
import { ProductRail } from "@/components/product-rail";
import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { StorySection } from "@/components/story-section";
import { getCatalog } from "@/lib/commerce-api";
import styles from "@/styles/storefront.module.css";

type HomeProps = {
  searchParams: Promise<{
    postal_code?: string;
    query?: string;
  }>;
};

export default async function Home({ searchParams }: HomeProps) {
  const params = await searchParams;
  const postalCode = params.postal_code?.trim() || "560038";
  const query = params.query?.trim();
  const { catalog, source } = await getCatalog({ postalCode, query });
  const sectionTitle = query ? `Results for “${query}”` : "Picked for today";

  return (
    <div className={styles.siteShell} data-catalog-source={source}>
      <SiteHeader postalCode={postalCode} />
      <main>
        <Hero />
        <ProductRail
          products={catalog.products.slice(0, 4)}
          title={sectionTitle}
        />
        <CategoryRail />
        <StorySection />
      </main>
      <SiteFooter />
    </div>
  );
}
