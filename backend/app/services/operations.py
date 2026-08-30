import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    AuditEvent,
    Checkout,
    InventoryItem,
    Location,
    Merchant,
    Order,
    Payment,
    Product,
    ProductVariant,
    UserAccount,
)
from app.domain.enums import CheckoutStatus, OrderStatus, PaymentStatus
from app.schemas.operations import (
    InventoryRowResponse,
    InventoryUpdateRequest,
    OperationsDashboardResponse,
    OperationsOrderResponse,
    OperationsSummaryResponse,
)

ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    # Payment capture is the only authority allowed to mark an order paid.
    OrderStatus.AWAITING_PAYMENT: {OrderStatus.CANCELED},
    OrderStatus.PAID: {OrderStatus.PREPARING, OrderStatus.CANCELED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.CANCELED},
    OrderStatus.READY: {OrderStatus.FULFILLED, OrderStatus.CANCELED},
    OrderStatus.FULFILLED: set(),
    OrderStatus.CANCELED: set(),
}


class OperationsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def dashboard(self, merchant_user: UserAccount) -> OperationsDashboardResponse:
        merchant = self._merchant(merchant_user.merchant_id)
        open_checkouts = (
            self.db.scalar(
                select(func.count())
                .select_from(Checkout)
                .where(
                    Checkout.merchant_id == merchant.id,
                    Checkout.status.in_([CheckoutStatus.OPEN, CheckoutStatus.READY_FOR_APPROVAL]),
                )
            )
            or 0
        )
        orders_preparing = (
            self.db.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.merchant_id == merchant.id, Order.status == OrderStatus.PREPARING)
            )
            or 0
        )
        low_stock = (
            self.db.scalar(
                select(func.count())
                .select_from(InventoryItem)
                .join(ProductVariant, InventoryItem.variant_id == ProductVariant.id)
                .where(
                    ProductVariant.merchant_id == merchant.id,
                    InventoryItem.on_hand_quantity - InventoryItem.reserved_quantity
                    <= InventoryItem.reorder_point,
                )
            )
            or 0
        )
        captured_revenue = (
            self.db.scalar(
                select(func.coalesce(func.sum(Payment.amount_minor), 0))
                .select_from(Payment)
                .join(Order, Payment.order_id == Order.id)
                .where(Order.merchant_id == merchant.id, Payment.status == PaymentStatus.CAPTURED)
            )
            or 0
        )
        return OperationsDashboardResponse(
            summary=OperationsSummaryResponse(
                open_checkouts=open_checkouts,
                orders_preparing=orders_preparing,
                low_stock_variants=low_stock,
                captured_revenue_minor=captured_revenue,
                currency=merchant.currency,
            ),
            recent_orders=self._orders(merchant.id, limit=10),
            inventory_attention=self._inventory(merchant.id, only_low_stock=True, limit=10),
        )

    def orders(self, merchant_user: UserAccount) -> list[OperationsOrderResponse]:
        return self._orders(self._merchant(merchant_user.merchant_id).id, limit=100)

    def inventory(self, merchant_user: UserAccount) -> list[InventoryRowResponse]:
        return self._inventory(
            self._merchant(merchant_user.merchant_id).id, only_low_stock=False, limit=500
        )

    def update_inventory(
        self,
        merchant_user: UserAccount,
        inventory_id: uuid.UUID,
        payload: InventoryUpdateRequest,
    ) -> InventoryRowResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_user.merchant_id)
            inventory = self.db.scalar(
                select(InventoryItem)
                .join(ProductVariant, InventoryItem.variant_id == ProductVariant.id)
                .where(
                    InventoryItem.id == inventory_id,
                    ProductVariant.merchant_id == merchant.id,
                )
                .with_for_update()
            )
            if inventory is None:
                raise NotFoundError("inventory_not_found", "Inventory item was not found.")
            if payload.on_hand_quantity < inventory.reserved_quantity:
                raise ConflictError(
                    "inventory_below_reserved",
                    "On-hand quantity cannot be lower than reserved quantity.",
                )
            before = inventory.on_hand_quantity
            inventory.on_hand_quantity = payload.on_hand_quantity
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="merchant_user",
                    actor_id=str(merchant_user.id),
                    event_type="inventory.adjusted",
                    aggregate_type="inventory_item",
                    aggregate_id=str(inventory.id),
                    payload={
                        "before_on_hand": before,
                        "after_on_hand": payload.on_hand_quantity,
                        "reason": payload.reason,
                    },
                )
            )
            self.db.flush()
            return self._inventory_row(merchant.id, inventory.id)

    def update_order_status(
        self, merchant_user: UserAccount, order_id: uuid.UUID, status: OrderStatus
    ) -> OperationsOrderResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_user.merchant_id)
            order = self.db.scalar(
                select(Order)
                .where(Order.id == order_id, Order.merchant_id == merchant.id)
                .with_for_update()
            )
            if order is None:
                raise NotFoundError("order_not_found", "Order was not found.")
            if status not in ORDER_TRANSITIONS[order.status]:
                raise ConflictError(
                    "invalid_order_transition",
                    f"Order cannot move from {order.status.value} to {status.value}.",
                )
            previous = order.status
            order.status = status
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="merchant_user",
                    actor_id=str(merchant_user.id),
                    event_type="order.status_changed",
                    aggregate_type="order",
                    aggregate_id=str(order.id),
                    payload={"from": previous.value, "to": status.value},
                )
            )
            self.db.flush()
            return self._order_row(merchant.id, order.id)

    def _orders(self, merchant_id: uuid.UUID, limit: int) -> list[OperationsOrderResponse]:
        order_ids = list(
            self.db.scalars(
                select(Order.id)
                .where(Order.merchant_id == merchant_id)
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
        )
        return [self._order_row(merchant_id, order_id) for order_id in order_ids]

    def _order_row(self, merchant_id: uuid.UUID, order_id: uuid.UUID) -> OperationsOrderResponse:
        row = self.db.execute(
            select(Order, Checkout.fulfillment_type, UserAccount.full_name)
            .join(Checkout, Order.checkout_id == Checkout.id)
            .outerjoin(UserAccount, Order.customer_id == UserAccount.id)
            .where(Order.id == order_id, Order.merchant_id == merchant_id)
        ).one()
        order, fulfillment_type, customer_name = row
        return OperationsOrderResponse(
            id=order.id,
            public_number=order.public_number,
            customer_name=customer_name,
            fulfillment_type=fulfillment_type.value,
            total_minor=order.total_minor,
            currency=order.currency,
            status=order.status,
            created_at=order.created_at,
        )

    def _inventory(
        self, merchant_id: uuid.UUID, only_low_stock: bool, limit: int
    ) -> list[InventoryRowResponse]:
        statement = (
            select(InventoryItem.id)
            .join(ProductVariant, InventoryItem.variant_id == ProductVariant.id)
            .where(ProductVariant.merchant_id == merchant_id)
            .order_by(
                (InventoryItem.on_hand_quantity - InventoryItem.reserved_quantity).asc(),
                InventoryItem.updated_at.desc(),
            )
            .limit(limit)
        )
        if only_low_stock:
            statement = statement.where(
                InventoryItem.on_hand_quantity - InventoryItem.reserved_quantity
                <= InventoryItem.reorder_point
            )
        ids = list(self.db.scalars(statement))
        return [self._inventory_row(merchant_id, inventory_id) for inventory_id in ids]

    def _inventory_row(
        self, merchant_id: uuid.UUID, inventory_id: uuid.UUID
    ) -> InventoryRowResponse:
        row = self.db.execute(
            select(InventoryItem, ProductVariant, Product.name, Location.name)
            .join(ProductVariant, InventoryItem.variant_id == ProductVariant.id)
            .join(Product, ProductVariant.product_id == Product.id)
            .join(Location, InventoryItem.location_id == Location.id)
            .where(InventoryItem.id == inventory_id, ProductVariant.merchant_id == merchant_id)
        ).one()
        inventory, variant, product_name, location_name = row
        return InventoryRowResponse(
            id=inventory.id,
            variant_id=variant.id,
            sku=variant.sku,
            product_name=product_name,
            variant_name=variant.name,
            location_name=location_name,
            on_hand_quantity=inventory.on_hand_quantity,
            reserved_quantity=inventory.reserved_quantity,
            available_quantity=inventory.on_hand_quantity - inventory.reserved_quantity,
            reorder_point=inventory.reorder_point,
        )

    def _merchant(self, merchant_id: uuid.UUID | None) -> Merchant:
        merchant = self.db.get(Merchant, merchant_id) if merchant_id else None
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant
