from typing import Any

from app.domain.enums import FulfillmentType


def supports_fulfillment(
    product_attributes: dict[str, Any], fulfillment_type: FulfillmentType
) -> bool:
    """Return whether a product may use a fulfillment rail.

    Catalog records created before fulfillment eligibility was introduced remain
    compatible. Once a record declares ``fulfillment_types``, malformed or
    unknown values fail closed.
    """

    configured = product_attributes.get("fulfillment_types")
    if configured is None:
        return True
    if not isinstance(configured, list):
        return False
    try:
        allowed = {FulfillmentType(value) for value in configured}
    except (TypeError, ValueError):
        return False
    return fulfillment_type in allowed
