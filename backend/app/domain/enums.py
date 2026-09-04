from enum import StrEnum


class MerchantStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class LocationKind(StrEnum):
    CAFE = "cafe"
    ROASTERY = "roastery"
    FULFILLMENT_HUB = "fulfillment_hub"


class ProductType(StrEnum):
    PREPARED_BEVERAGE = "prepared_beverage"
    PACKAGED_COFFEE = "packaged_coffee"
    PACKAGED_TEA = "packaged_tea"
    ACCESSORY = "accessory"


class ProductStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class FulfillmentType(StrEnum):
    PICKUP = "pickup"
    LOCAL_DELIVERY = "local_delivery"
    SHIPPING = "shipping"


class CheckoutStatus(StrEnum):
    OPEN = "open"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVED = "approved"
    PAYMENT_PENDING = "payment_pending"
    COMPLETED = "completed"
    CANCELED = "canceled"
    EXPIRED = "expired"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class OrderStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PREPARING = "preparing"
    READY = "ready"
    FULFILLED = "fulfilled"
    CANCELED = "canceled"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class PurchaseIntentStatus(StrEnum):
    DRAFT = "draft"
    PENDING_PROVIDER_AUTHORIZATION = "pending_provider_authorization"
    ACTIVE = "active"
    PAUSED = "paused"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ScheduledRunStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CHECKOUT_CREATED = "checkout_created"
    NOTIFICATION_PENDING = "notification_pending"
    PAYMENT_PENDING = "payment_pending"
    SUCCEEDED = "succeeded"
    REQUIRES_HUMAN_ACTION = "requires_human_action"
    FAILED = "failed"
    SKIPPED = "skipped"


class UserRole(StrEnum):
    CUSTOMER = "customer"
    MERCHANT_ADMIN = "merchant_admin"
    MERCHANT_STAFF = "merchant_staff"


class CartStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    ABANDONED = "abandoned"
