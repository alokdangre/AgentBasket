import {
  type CatalogResponse,
  demoCatalog,
} from "@/lib/storefront-data";

const commerceApiUrl =
  process.env.COMMERCE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

type CatalogQuery = {
  postalCode?: string;
  query?: string;
};

export type CatalogResult = {
  catalog: CatalogResponse;
  source: "api" | "demo";
};

export async function getCatalog({
  postalCode = "560038",
  query,
}: CatalogQuery = {}): Promise<CatalogResult> {
  const search = new URLSearchParams({ postal_code: postalCode });
  if (query?.trim()) {
    search.set("query", query.trim());
  }

  try {
    const response = await fetch(
      `${commerceApiUrl}/api/v1/merchants/ember-and-leaf/catalog?${search}`,
      {
        next: { revalidate: 60 },
        signal: AbortSignal.timeout(2500),
      },
    );
    if (!response.ok) {
      throw new Error(`Catalog request failed with status ${response.status}`);
    }
    return {
      catalog: (await response.json()) as CatalogResponse,
      source: "api",
    };
  } catch {
    const normalizedQuery = query?.trim().toLocaleLowerCase();
    const fallbackProducts = normalizedQuery
      ? demoCatalog.products.filter((item) =>
          `${item.name} ${item.description}`
            .toLocaleLowerCase()
            .includes(normalizedQuery),
        )
      : demoCatalog.products;
    return {
      catalog: {
        ...demoCatalog,
        postal_code: postalCode,
        products: fallbackProducts,
      },
      source: "demo",
    };
  }
}
