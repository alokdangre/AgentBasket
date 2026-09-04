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

export type Passkey = {
  id: string;
  label: string;
  device_type: string;
  backed_up: boolean;
  created_at: string;
  last_used_at: string | null;
};

export type PaymentInstrument = {
  id: string;
  provider: string;
  instrument_type: string;
  alias: string;
  network: string | null;
  last4: string | null;
  is_default: boolean;
  requires_provider_checkout: boolean;
  recurring_ready: boolean;
  recurring_status: string | null;
};

export type RazorpayRecurringAuthorizationSession = {
  scheduled_purchase_id: string;
  payment_instrument_id: string;
  key_id: string;
  provider_order_id: string;
  provider_customer_id: string;
  amount_minor: number;
  max_amount_minor: number;
  currency: string;
  mandate_expires_at: string;
  merchant_name: string;
  description: string;
  customer_name: string;
  customer_email: string;
  customer_phone: string;
  recurring: true;
};

export type RazorpayRecurringAuthorizationResult = {
  scheduled_purchase_id: string;
  payment_instrument_id: string;
  status: string;
  provider_payment_id: string;
  token_confirmation_pending: boolean;
};

export type PurchaseIntentStatus =
  | "draft"
  | "pending_provider_authorization"
  | "active"
  | "paused"
  | "needs_attention"
  | "completed"
  | "expired"
  | "revoked";

export type ScheduledRunStatus =
  | "pending"
  | "claimed"
  | "checkout_created"
  | "notification_pending"
  | "payment_pending"
  | "succeeded"
  | "requires_human_action"
  | "failed"
  | "skipped";

export type ScheduleFrequency = "once" | "daily" | "weekly" | "monthly";

export type ScheduledPurchaseRun = {
  id: string;
  scheduled_for: string;
  status: ScheduledRunStatus;
  attempt_count: number;
  checkout_id: string | null;
  order_id: string | null;
  payment_id: string | null;
  amount_minor: number;
  currency: string;
  provider_payment_after: string | null;
  provider_order_id: string | null;
  provider_payment_id: string | null;
  failure_code: string | null;
  failure_message: string | null;
  evidence: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ScheduledPurchase = {
  id: string;
  merchant_slug: string;
  status: PurchaseIntentStatus;
  fulfillment_type: "pickup" | "local_delivery" | "shipping";
  address_id: string | null;
  location_id: string | null;
  payment_instrument_id: string;
  payment_instrument_alias: string;
  constraints: Record<string, unknown>;
  frequency: ScheduleFrequency;
  interval_count: number;
  timezone: string;
  next_run_at: string | null;
  next_execution_at: string | null;
  expires_at: string;
  max_occurrences: number;
  successful_occurrences: number;
  max_amount_minor: number;
  max_total_minor: number;
  spent_minor: number;
  currency: string;
  display: Record<string, unknown> | null;
  display_sha256: string | null;
  open_checkout_hash: string | null;
  authorization_reference: string | null;
  provider_ready: boolean;
  authorized_at: string | null;
  provider_authorized_at: string | null;
  paused_at: string | null;
  revoked_at: string | null;
  last_failure_code: string | null;
  last_failure_message: string | null;
  created_at: string;
  updated_at: string;
  runs: ScheduledPurchaseRun[];
};

export type ScheduledPurchaseList = {
  scheduled_purchases: ScheduledPurchase[];
};

export type ScheduledPurchaseChallenge = {
  intent_id: string;
  nonce: string;
  display_sha256: string;
  display: Record<string, unknown>;
  expires_at: string;
  agent_public_jwk: Record<string, unknown>;
  webauthn_options: PublicKeyCredentialRequestOptionsJSON;
};

export type ScheduledPurchaseAuthorization = {
  scheduled_purchase: ScheduledPurchase;
  open_checkout_mandate: string;
  open_payment_mandate: string;
};

export type ScheduledPurchaseAction = {
  id: string;
  status: PurchaseIntentStatus;
  next_run_at: string | null;
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
  source: "storefront" | "agent" | "scheduled_agent";
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
  scheduled_purchase?: ScheduledPurchase;
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
  webauthn_options: PublicKeyCredentialRequestOptionsJSON;
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
  credential_grant: {
    id: string;
    credential_kind: string;
    instrument_alias: string;
    status: string;
    expires_at: string;
  };
};

export type AP2Evidence = {
  checkout_id: string;
  challenge_id: string;
  checkout_hash: string;
  status: "accepted";
  mandates: AP2MandateEvidence[];
  receipts: Array<{
    id: string;
    order_id: string;
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
