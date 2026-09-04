export type ModifierOption = {
  id: string;
  name: string;
  price_delta_minor: number;
};

export type ModifierGroup = {
  id: string;
  name: string;
  required: boolean;
  minimum_selections: number;
  maximum_selections: number;
  options: ModifierOption[];
};

export type ProductVariant = {
  id: string;
  sku: string;
  name: string;
  price_minor: number;
  currency: string;
  size_label: string | null;
  weight_grams: number | null;
  preparation_minutes: number;
  available_quantity: number | null;
  attributes: Record<string, unknown>;
};

export type CatalogProduct = {
  id: string;
  slug: string;
  name: string;
  description: string;
  product_type:
    | "prepared_beverage"
    | "packaged_coffee"
    | "packaged_tea"
    | "accessory";
  attributes: Record<string, unknown>;
  image_urls: string[];
  variants: ProductVariant[];
  modifier_groups: ModifierGroup[];
};

export type CatalogResponse = {
  merchant_slug: string;
  location_id: string | null;
  postal_code: string | null;
  currency: string;
  products: CatalogProduct[];
};

const product = (
  slug: string,
  name: string,
  description: string,
  productType: CatalogProduct["product_type"],
  priceMinor: number,
  variantName: string,
): CatalogProduct => ({
  id: `demo-${slug}`,
  slug,
  name,
  description,
  product_type: productType,
  attributes: {},
  image_urls: [],
  modifier_groups: [],
  variants: [
    {
      id: `demo-${slug}-variant`,
      sku: `DEMO-${slug.toUpperCase()}`,
      name: variantName,
      price_minor: priceMinor,
      currency: "INR",
      size_label: variantName,
      weight_grams: null,
      preparation_minutes: productType === "prepared_beverage" ? 10 : 0,
      available_quantity: null,
      attributes: {},
    },
  ],
});

export const demoCatalog: CatalogResponse = {
  merchant_slug: "ember-and-leaf",
  location_id: null,
  postal_code: "560038",
  currency: "INR",
  products: [
    product(
      "house-cold-brew",
      "House Cold Brew",
      "Slow-steeped for a smooth cocoa finish.",
      "prepared_beverage",
      22000,
      "Regular",
    ),
    product(
      "citrus-bloom-coffee",
      "Citrus Bloom Single-Origin Coffee",
      "Orange blossom, peach and caramel from Chikmagalur.",
      "packaged_coffee",
      65000,
      "250 g",
    ),
    product(
      "masala-cloud-chai",
      "Masala Cloud Chai",
      "Assam tea, ginger and warm spices steamed to order.",
      "prepared_beverage",
      18000,
      "Regular",
    ),
    product(
      "darjeeling-first-flush",
      "Darjeeling First Flush",
      "A floral loose-leaf tea with muscatel sweetness.",
      "packaged_tea",
      78000,
      "100 g pouch",
    ),
    product(
      "v60-filter-papers",
      "V60 Filter Papers",
      "Oxygen-bleached size 02 paper filters, pack of 100.",
      "accessory",
      35000,
      "Pack of 100",
    ),
    product(
      "bengaluru-filter-coffee",
      "Bengaluru Filter Coffee",
      "South Indian filter coffee with a deep roast and silky milk.",
      "prepared_beverage",
      16000,
      "Regular",
    ),
    product(
      "hibiscus-citrus-iced-tea",
      "Hibiscus Citrus Iced Tea",
      "Tart hibiscus, orange and lemongrass shaken over ice.",
      "prepared_beverage",
      21000,
      "Regular",
    ),
    product(
      "sparkling-kokum-cooler",
      "Sparkling Kokum Cooler",
      "Kokum, lime and soda with a lightly salted, tangy finish.",
      "prepared_beverage",
      24000,
      "Regular",
    ),
    product(
      "salted-jaggery-hot-chocolate",
      "Salted Jaggery Hot Chocolate",
      "Dark cocoa, jaggery and sea salt in a rich steamed drink.",
      "prepared_beverage",
      26000,
      "Regular",
    ),
  ],
};

export const productImages: Record<string, string> = {
  "house-cold-brew": "/images/house-cold-brew.webp",
  "citrus-bloom-coffee": "/images/citrus-bloom.webp",
  "masala-cloud-chai": "/images/masala-cloud-chai.webp",
  "darjeeling-first-flush": "/images/darjeeling-first-flush.webp",
  "v60-filter-papers": "/images/roastery.webp",
  "bengaluru-filter-coffee": "/images/masala-cloud-chai.webp",
  "hibiscus-citrus-iced-tea": "/images/darjeeling-first-flush.webp",
  "sparkling-kokum-cooler": "/images/house-cold-brew.webp",
  "salted-jaggery-hot-chocolate": "/images/masala-cloud-chai.webp",
};

export const productDisplayNames: Record<string, string> = {
  "citrus-bloom-coffee": "Citrus Bloom",
};

export function formatMoney(amountMinor: number, currency = "INR"): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
  }).format(amountMinor / 100);
}
