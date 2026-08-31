import type { ModifierGroup, ProductVariant } from "@/lib/storefront-data";

export type UserRole = "customer" | "merchant_admin" | "merchant_staff";

export type SessionUser = {
  id: string;
  email: string;
  full_name: string;
  phone: string | null;
  role: UserRole;
  merchant_id: string | null;
};

export type Address = {
  id: string;
  label: string;
  recipient_name: string;
  phone: string;
  line_one: string;
  line_two: string | null;
  landmark: string | null;
  city: string;
  region: string;
  postal_code: string;
  country_code: string;
  is_default: boolean;
};

export type CartModifier = {
  id: string;
  name: string;
  price_delta_minor: number;
};

export type CartItem = {
  id: string;
  variant_id: string;
  product_slug: string;
  product_name: string;
  variant_name: string;
  sku: string;
  image_urls: string[];
  quantity: number;
  unit_price_minor: number;
  modifiers: CartModifier[];
  line_total_minor: number;
  available_quantity: number | null;
};

export type Cart = {
  id: string;
  merchant_slug: string;
  status: "active" | "converted" | "abandoned";
  currency: string;
  item_count: number;
  subtotal_minor: number;
  items: CartItem[];
};

export type CheckoutLine = {
  id: string;
  variant_id: string;
  product_name: string;
  variant_name: string;
  quantity: number;
  unit_price_minor: number;
  modifier_total_minor: number;
  line_total_minor: number;
  modifiers: CartModifier[];
};

export type Checkout = {
  id: string;
  merchant_id: string;
  customer_id: string;
  location_id: string;
  status:
    | "open"
    | "ready_for_approval"
    | "approved"
    | "payment_pending"
    | "completed"
    | "canceled"
    | "expired";
  currency: string;
  fulfillment_type: string;
  postal_code: string | null;
  lines: CheckoutLine[];
  fulfillment_options: Array<{
    id: string;
    fulfillment_type: string;
    title: string;
    fee_minor: number;
    eta_min_minutes: number;
    eta_max_minutes: number;
    selected: boolean;
  }>;
  subtotal_minor: number;
  delivery_minor: number;
  discount_minor: number;
  tax_minor: number;
  tax_included: boolean;
  total_minor: number;
  quote_version: number;
  expires_at: string;
  source: "storefront" | "agent";
};

export type CheckoutApproval = {
  id: string;
  checkout_id: string;
  quote_version: number;
  approved_total_minor: number;
  currency: string;
  evidence_sha256: string;
  approved_at: string;
};

export type RazorpaySession = {
  checkout_id: string;
  order_id: string;
  public_number: string;
  key_id: string;
  provider_order_id: string;
  amount_minor: number;
  currency: string;
  merchant_name: string;
  description: string;
  customer_name: string;
  customer_email: string;
  customer_phone: string | null;
};

export type OrderReceipt = {
  id: string;
  public_number: string;
  checkout_id: string;
  checkout_status: Checkout["status"];
  status: OperationsOrder["status"];
  total_minor: number;
  currency: string;
  fulfillment: {
    type?: string;
    postal_code?: string | null;
    delivery_address?: Record<string, string | null> | null;
    title?: string;
    eta_min_minutes?: number | null;
    eta_max_minutes?: number | null;
  };
  created_at: string;
  payment: {
    status: "created" | "authorized" | "captured" | "failed" | "refunded";
    provider: string;
    provider_order_id: string | null;
    provider_payment_id: string | null;
    amount_minor: number;
    currency: string;
    captured_at: string | null;
  };
  timeline: Array<{
    event_type: string;
    actor_type: string;
    occurred_at: string;
    payload: Record<string, unknown>;
  }>;
};

export type OrderSummary = Pick<
  OrderReceipt,
  "id" | "public_number" | "status" | "total_minor" | "currency" | "created_at"
>;

export type InventoryRow = {
  id: string;
  variant_id: string;
  sku: string;
  product_name: string;
  variant_name: string;
  location_name: string;
  on_hand_quantity: number;
  reserved_quantity: number;
  available_quantity: number;
  reorder_point: number;
};

export type OperationsOrder = {
  id: string;
  public_number: string;
  customer_name: string | null;
  fulfillment_type: string;
  total_minor: number;
  currency: string;
  status:
    | "awaiting_payment"
    | "paid"
    | "preparing"
    | "ready"
    | "fulfilled"
    | "canceled";
  created_at: string;
};

export type OperationsDashboard = {
  summary: {
    open_checkouts: number;
    orders_preparing: number;
    low_stock_variants: number;
    captured_revenue_minor: number;
    currency: string;
  };
  recent_orders: OperationsOrder[];
  inventory_attention: InventoryRow[];
};

export type ApiError = {
  error?: { code?: string; message?: string };
  detail?: string | Array<{ msg?: string }>;
};

export type AgentProduct = {
  id: string;
  slug: string;
  name: string;
  description: string;
  product_type: "prepared_beverage" | "packaged_coffee" | "packaged_tea" | "accessory";
  attributes: Record<string, unknown>;
  image_url: string | null;
  variants: ProductVariant[];
  modifier_groups: ModifierGroup[];
  recommendation_reason?: string;
};

export type AgentStructuredContent = {
  suggestions?: string[];
  products?: AgentProduct[];
  cart?: Cart;
  checkout?: Checkout & {
    approval_required: boolean;
    payment_started: boolean;
    review_url: string;
  };
  destinations?: {
    delivery_addresses: Array<{
      id: string;
      label: string;
      city: string;
      region: string;
      postal_code: string;
      is_default: boolean;
    }>;
    pickup_locations: Array<{
      id: string;
      name: string;
      postal_code: string;
      preparation_minutes: number;
    }>;
  };
  activity?: Array<{ tool: string; status: "success" | "error"; label: string }>;
};

export type AgentMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  structured_content: AgentStructuredContent;
  model: string | null;
  created_at: string;
};

export type AgentConversation = {
  id: string;
  merchant_slug: string;
  title: string;
  status: string;
  messages: AgentMessage[];
  created_at: string;
  last_activity_at: string;
};

export type AgentTurn = {
  conversation_id: string;
  run_id: string;
  message: AgentMessage;
};

export type AP2Challenge = {
  id: string;
  checkout_id: string;
  nonce: string;
  checkout_hash: string;
  display_sha256: string;
  expires_at: string;
  display: {
    total_minor: number;
    currency: string;
    quote_version: number;
    fulfillment: {
      title: string;
      postal_code: string | null;
      eta_min_minutes: number | null;
      eta_max_minutes: number | null;
    };
  };
  checkout_mandate: { vct: "mandate.checkout.1" };
  payment_mandate: { vct: "mandate.payment.1" };
};

export type AP2MandateEvidence = {
  id: string;
  mandate_type: "checkout" | "payment";
  vct: string;
  issuer: string;
  key_id: string;
  checkout_hash: string;
  verification_status: "verified";
  signed_jwt: string;
};

export type AP2Approval = {
  challenge_id: string;
  checkout_hash: string;
  approval: CheckoutApproval;
  mandates: AP2MandateEvidence[];
};

export type AP2Evidence = {
  checkout_id: string;
  challenge_id: string;
  checkout_hash: string;
  status: "accepted";
  mandates: AP2MandateEvidence[];
  receipts: Array<{
    id: string;
    receipt_type: "checkout" | "payment";
    status: "Success";
    issuer: string;
    key_id: string;
    reference: string;
    signed_jwt: string;
    created_at: string;
  }>;
};

export function apiErrorMessage(payload: ApiError, fallback: string): string {
  if (payload.error?.message) return payload.error.message;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail) && payload.detail[0]?.msg) return payload.detail[0].msg;
  return fallback;
}
