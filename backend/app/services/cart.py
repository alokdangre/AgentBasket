import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    Cart,
    CartItem,
    InventoryItem,
    Merchant,
    ModifierGroup,
    ModifierOption,
    Product,
    ProductVariant,
    UserAccount,
)
from app.domain.enums import CartStatus, ProductStatus
from app.schemas.cart import (
    CartItemCreateRequest,
    CartItemResponse,
    CartItemUpdateRequest,
    CartModifierResponse,
    CartResponse,
)


class CartService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user: UserAccount, merchant_slug: str) -> CartResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            cart = self._cart(user.id, merchant, create=True)
            return self._to_response(cart, merchant)

    def add_item(
        self, user: UserAccount, merchant_slug: str, payload: CartItemCreateRequest
    ) -> CartResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            cart = self._cart(user.id, merchant, create=True)
            variant = self._variant(merchant.id, payload.variant_id)
            modifiers = self._validate_modifiers(variant.product, payload.modifier_option_ids)
            signature = self._modifier_signature(payload.modifier_option_ids)
            item = self.db.scalar(
                select(CartItem).where(
                    CartItem.cart_id == cart.id,
                    CartItem.variant_id == variant.id,
                    CartItem.modifier_signature == signature,
                )
            )
            requested_quantity = payload.quantity + (item.quantity if item else 0)
            if requested_quantity > 25:
                raise DomainError("cart_quantity_limit", "Cart item quantity cannot exceed 25.")
            self._ensure_available(variant, requested_quantity)
            if item is None:
                cart.items.append(
                    CartItem(
                        variant_id=variant.id,
                        quantity=payload.quantity,
                        modifier_option_ids=[str(option.id) for option in modifiers],
                        modifier_signature=signature,
                    )
                )
            else:
                item.quantity = requested_quantity
            cart.status = CartStatus.ACTIVE
            self.db.flush()
            return self._to_response(cart, merchant)

    def update_item(
        self,
        user: UserAccount,
        merchant_slug: str,
        item_id: uuid.UUID,
        payload: CartItemUpdateRequest,
    ) -> CartResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            cart = self._cart(user.id, merchant, create=False)
            item = self._item(cart.id, item_id)
            variant = self._variant(merchant.id, item.variant_id)
            self._ensure_available(variant, payload.quantity)
            item.quantity = payload.quantity
            self.db.flush()
            return self._to_response(cart, merchant)

    def remove_item(
        self, user: UserAccount, merchant_slug: str, item_id: uuid.UUID
    ) -> CartResponse:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            cart = self._cart(user.id, merchant, create=False)
            self.db.delete(self._item(cart.id, item_id))
            self.db.flush()
            return self._to_response(cart, merchant)

    def _merchant(self, slug: str) -> Merchant:
        merchant = self.db.scalar(select(Merchant).where(Merchant.slug == slug))
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant

    def _cart(self, user_id: uuid.UUID, merchant: Merchant, create: bool) -> Cart:
        cart = self.db.scalar(
            select(Cart)
            .where(Cart.user_id == user_id, Cart.merchant_id == merchant.id)
            .options(selectinload(Cart.items))
        )
        if cart is None and create:
            cart = Cart(
                user_id=user_id,
                merchant_id=merchant.id,
                status=CartStatus.ACTIVE,
                currency=merchant.currency,
            )
            self.db.add(cart)
            self.db.flush()
        if cart is None:
            raise NotFoundError("cart_not_found", "Cart was not found.")
        return cart

    def _variant(self, merchant_id: uuid.UUID, variant_id: uuid.UUID) -> ProductVariant:
        variant = self.db.scalar(
            select(ProductVariant)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(
                ProductVariant.id == variant_id,
                ProductVariant.merchant_id == merchant_id,
                Product.merchant_id == merchant_id,
                ProductVariant.sellable.is_(True),
                Product.status == ProductStatus.ACTIVE,
            )
            .options(
                selectinload(ProductVariant.product)
                .selectinload(Product.modifier_groups)
                .selectinload(ModifierGroup.options)
            )
        )
        if variant is None:
            raise NotFoundError("variant_not_found", "Product variant is unavailable.")
        return variant

    @staticmethod
    def _validate_modifiers(
        product: Product, selected_ids: list[uuid.UUID]
    ) -> list[ModifierOption]:
        if len(selected_ids) != len(set(selected_ids)):
            raise DomainError("duplicate_modifier", "A modifier was selected more than once.")
        available = {
            option.id: option
            for group in product.modifier_groups
            for option in group.options
            if option.active
        }
        selected = set(selected_ids)
        if selected - set(available):
            raise DomainError("invalid_modifier", "A modifier does not belong to this product.")
        for group in product.modifier_groups:
            count = sum(option.id in selected for option in group.options)
            if count < group.minimum_selections or (group.required and count == 0):
                raise DomainError(
                    "required_modifier_missing", f"Select an option for {group.name}."
                )
            if count > group.maximum_selections:
                raise DomainError("too_many_modifiers", f"Too many options for {group.name}.")
        return [available[option_id] for option_id in selected_ids]

    def _ensure_available(self, variant: ProductVariant, quantity: int) -> None:
        if not variant.track_inventory:
            return
        inventory = list(
            self.db.scalars(select(InventoryItem).where(InventoryItem.variant_id == variant.id))
        )
        available = sum(item.on_hand_quantity - item.reserved_quantity for item in inventory)
        if available < quantity:
            raise ConflictError("insufficient_inventory", "Requested quantity is unavailable.")

    @staticmethod
    def _modifier_signature(option_ids: list[uuid.UUID]) -> str:
        canonical = ",".join(sorted(str(option_id) for option_id in option_ids))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _item(self, cart_id: uuid.UUID, item_id: uuid.UUID) -> CartItem:
        item = self.db.scalar(
            select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart_id)
        )
        if item is None:
            raise NotFoundError("cart_item_not_found", "Cart item was not found.")
        return item

    def _to_response(self, cart: Cart, merchant: Merchant) -> CartResponse:
        if not cart.items:
            return CartResponse(
                id=cart.id,
                merchant_slug=merchant.slug,
                status=cart.status,
                currency=cart.currency,
                item_count=0,
                subtotal_minor=0,
                items=[],
            )
        variant_ids = {item.variant_id for item in cart.items}
        variants = {
            variant.id: variant
            for variant in self.db.scalars(
                select(ProductVariant)
                .where(ProductVariant.id.in_(variant_ids))
                .options(
                    selectinload(ProductVariant.product),
                    selectinload(ProductVariant.product)
                    .selectinload(Product.modifier_groups)
                    .selectinload(ModifierGroup.options),
                )
            ).unique()
        }
        inventories: dict[uuid.UUID, int] = {}
        for inventory in self.db.scalars(
            select(InventoryItem).where(InventoryItem.variant_id.in_(variant_ids))
        ):
            inventories[inventory.variant_id] = inventories.get(inventory.variant_id, 0) + (
                inventory.on_hand_quantity - inventory.reserved_quantity
            )
        output_items: list[CartItemResponse] = []
        subtotal = 0
        item_count = 0
        for item in cart.items:
            variant = variants.get(item.variant_id)
            if variant is None:
                continue
            options = {
                str(option.id): option
                for group in variant.product.modifier_groups
                for option in group.options
            }
            selected = [
                options[option_id] for option_id in item.modifier_option_ids if option_id in options
            ]
            unit_total = variant.price_minor + sum(option.price_delta_minor for option in selected)
            line_total = unit_total * item.quantity
            subtotal += line_total
            item_count += item.quantity
            output_items.append(
                CartItemResponse(
                    id=item.id,
                    variant_id=variant.id,
                    product_slug=variant.product.slug,
                    product_name=variant.product.name,
                    variant_name=variant.name,
                    sku=variant.sku,
                    image_urls=variant.product.image_urls,
                    quantity=item.quantity,
                    unit_price_minor=variant.price_minor,
                    modifiers=[
                        CartModifierResponse(
                            id=option.id,
                            name=option.name,
                            price_delta_minor=option.price_delta_minor,
                        )
                        for option in selected
                    ],
                    line_total_minor=line_total,
                    available_quantity=(
                        inventories.get(variant.id) if variant.track_inventory else None
                    ),
                )
            )
        return CartResponse(
            id=cart.id,
            merchant_slug=merchant.slug,
            status=cart.status,
            currency=cart.currency,
            item_count=item_count,
            subtotal_minor=subtotal,
            items=output_items,
        )
