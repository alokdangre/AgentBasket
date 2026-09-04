import type { CatalogProduct, CatalogResponse } from "@/lib/storefront-data";
import { productImages } from "@/lib/storefront-data";

type CommerceJsonLdProps = {
  catalog: CatalogResponse;
  products?: CatalogProduct[];
};

const storefrontUrl = (
  process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"
).replace(/\/$/, "");

function absoluteUrl(path: string): string {
  return path.startsWith("http://") || path.startsWith("https://")
    ? path
    : `${storefrontUrl}${path.startsWith("/") ? "" : "/"}${path}`;
}

function productUrl(product: CatalogProduct): string {
  return `${storefrontUrl}/shop?product=${encodeURIComponent(product.slug)}`;
}

function formatPrice(amountMinor: number): string {
  return (amountMinor / 100).toFixed(2);
}

function additionalProperties(product: CatalogProduct) {
  return Object.entries(product.attributes).flatMap(([name, value]) => {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return [{ "@type": "PropertyValue", name, value }];
    }
    if (Array.isArray(value)) {
      return [{ "@type": "PropertyValue", name, value: value.join(", ") }];
    }
    return [];
  });
}

export function buildCommerceJsonLd(
  catalog: CatalogResponse,
  products: CatalogProduct[] = catalog.products,
) {
  const merchantId = `${storefrontUrl}/#merchant`;
  const catalogId = `${storefrontUrl}/shop#catalog`;
  const productNodes = products.map((product) => {
    const url = productUrl(product);
    const mappedImage = productImages[product.slug];
    return {
      "@id": `${url}#product`,
      "@type": "Product",
      name: product.name,
      description: product.description,
      url,
      ...(mappedImage ? { image: [absoluteUrl(mappedImage)] } : {}),
      category: product.product_type,
      brand: { "@id": merchantId },
      additionalProperty: additionalProperties(product),
      offers: product.variants.map((variant) => ({
        "@id": `${url}#offer-${encodeURIComponent(variant.sku)}`,
        "@type": "Offer",
        sku: variant.sku,
        name: `${product.name} — ${variant.name}`,
        url: `${url}&variant=${encodeURIComponent(variant.id)}`,
        price: formatPrice(variant.price_minor),
        priceCurrency: variant.currency,
        availability:
          variant.available_quantity === 0
            ? "https://schema.org/OutOfStock"
            : "https://schema.org/InStock",
        itemCondition: "https://schema.org/NewCondition",
        seller: { "@id": merchantId },
      })),
    };
  });

  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@id": merchantId,
        "@type": ["Organization", "CafeOrCoffeeShop", "OnlineStore"],
        name: "Ember & Leaf",
        url: storefrontUrl,
        description:
          "A Bengaluru café and specialty roastery serving prepared drinks, coffee, tea and brewing goods.",
        address: {
          "@type": "PostalAddress",
          streetAddress: "100 Feet Road",
          addressLocality: "Bengaluru",
          addressRegion: "Karnataka",
          postalCode: "560038",
          addressCountry: "IN",
        },
        currenciesAccepted: catalog.currency,
        hasOfferCatalog: { "@id": catalogId },
      },
      {
        "@id": catalogId,
        "@type": "OfferCatalog",
        name: "Ember & Leaf catalog",
        url: `${storefrontUrl}/shop`,
        itemListElement: productNodes.map((product, index) => ({
          "@type": "ListItem",
          position: index + 1,
          item: { "@id": product["@id"] },
        })),
      },
      ...productNodes,
    ],
  };
}

export function CommerceJsonLd({ catalog, products = catalog.products }: CommerceJsonLdProps) {
  const jsonLd = buildCommerceJsonLd(catalog, products);
  return (
    <script
      id="ember-and-leaf-commerce-jsonld"
      type="application/ld+json"
      dangerouslySetInnerHTML={{
        __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
      }}
    />
  );
}
