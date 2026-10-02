"""How a Shopify order maps onto Castor's ClickHouse row.

**This is a transcription of ``castor-app/infra/lambda/import-worker/handler.py``**
-- specifically its ``order_row`` and the ``money_amount`` helper beside it. That
is deliberate rather than incidental: the Otter job and the Lambda write the same
table, so if the two disagreed about a value, the destination would hold
whichever ran last. Field order, nullable handling and the money type are all
copied from there, and the tests assert them.

The consequence is that this file is *not* idiomatic Python for a mapping. It
indexes dictionaries directly where the Lambda does, so a field Shopify stops
returning raises ``KeyError`` rather than quietly becoming an empty string -- a
schema change should fail a run, not write blank rows. It uses ``.get()`` exactly
where the Lambda does, for the fields Shopify marks nullable.

Two details are easy to "clean up" and must not be. ``cancelled`` is derived
from ``cancelledAt`` while ``cancelled_at`` is that same instant parsed: both
columns exist, and the flag answers "is this cancelled" while the timestamp
answers "when". And ``money_amount`` returns ``Decimal("0")`` rather than
``None`` when a money set is absent, because every money column is a
non-nullable ``Decimal(18, 2)`` -- a ``None`` there is a failed insert, not a
NULL.

It is pure: no environment reads, no I/O, and nothing imported from ``main`` or
``source``. The three tenant ids arrive as an argument.

Values are Python types -- ``str``, ``list``, ``bool``, ``int``, ``Decimal``,
``datetime``, ``None``. Turning those into a wire format is the client's job, not
this module's, which is why a ``Decimal`` stays a ``Decimal`` here instead of
being flattened to a float.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = [
    "ORDER_COLUMNS",
    "Tenancy",
    "empty_to_none",
    "money_amount",
    "order_row",
    "parse_datetime",
]


@dataclass(frozen=True)
class Tenancy:
    """The three Castor ids that identify *which* dataset a row belongs to.

    One job instance syncs one dataset, so these are configuration rather than
    per-record data -- which is exactly why they can be wrong without anything
    failing. In the Lambda they arrive on the SQS message; here they come from
    the manifest's ``env``.
    """

    workspace_id: str
    connection_id: str
    dataset_id: str


#: The destination columns, in schema order, copied from ``handler.py``.
ORDER_COLUMNS = (
    "workspace_id", "connection_id", "dataset_id", "shopify_order_id",
    "order_name", "order_email", "order_phone", "shopify_customer_id",
    "customer_first_name", "customer_last_name", "customer_email",
    "customer_phone", "financial_status", "fulfillment_status", "tags",
    "test", "cancelled", "subtotal_price", "discount_amount",
    "shipping_amount", "tax_amount", "total_price", "refunded_amount",
    "currency_code", "shipping_name", "shipping_company", "shipping_phone",
    "shipping_address1", "shipping_address2", "shipping_city",
    "shipping_province", "shipping_province_code", "shipping_postal_code",
    "shipping_country", "shipping_country_code", "shipping_latitude",
    "shipping_longitude", "billing_name", "billing_company", "billing_phone",
    "billing_address1", "billing_address2", "billing_city", "billing_province",
    "billing_province_code", "billing_postal_code", "billing_country",
    "billing_country_code", "created_at", "processed_at", "updated_at",
    "cancelled_at", "closed_at", "synced_at",
)


def order_row(order, synced_at, tenancy):
    """One ``shopify_orders`` row, keyed by the destination's column names.

    The direct indexes are the Lambda's: a field Shopify stops returning is a
    failed run rather than a row with a blank column. The ``.get()`` calls are
    also the Lambda's, for the fields Shopify marks nullable.

    ``cancelled`` is derived -- ``True`` whenever ``cancelledAt`` is present --
    and ``cancelled_at`` is that same value parsed. Both columns exist on
    purpose; the flag is what a filter reads, the timestamp is what a report
    groups by.
    """
    customer = order.get("customer") or {}
    customer_email = customer.get("defaultEmailAddress") or {}
    customer_phone = customer.get("defaultPhoneNumber") or {}
    shipping = order.get("shippingAddress") or {}
    billing = order.get("billingAddress") or {}
    total_money = order["currentTotalPriceSet"]["shopMoney"]
    return {
        "workspace_id": tenancy.workspace_id,
        "connection_id": tenancy.connection_id,
        "dataset_id": tenancy.dataset_id,
        "shopify_order_id": order["id"],
        "order_name": order["name"],
        "order_email": empty_to_none(order.get("email")),
        "order_phone": empty_to_none(order.get("phone")),
        "shopify_customer_id": empty_to_none(customer.get("id")),
        "customer_first_name": empty_to_none(customer.get("firstName")),
        "customer_last_name": empty_to_none(customer.get("lastName")),
        "customer_email": empty_to_none(customer_email.get("emailAddress")),
        "customer_phone": empty_to_none(customer_phone.get("phoneNumber")),
        "financial_status": order.get("displayFinancialStatus"),
        "fulfillment_status": order["displayFulfillmentStatus"],
        "tags": order["tags"],
        "test": order["test"],
        "cancelled": order.get("cancelledAt") is not None,
        "subtotal_price": money_amount(order, "currentSubtotalPriceSet"),
        "discount_amount": money_amount(order, "currentTotalDiscountsSet"),
        "shipping_amount": money_amount(order, "currentShippingPriceSet"),
        "tax_amount": money_amount(order, "currentTotalTaxSet"),
        "total_price": Decimal(total_money["amount"]),
        "refunded_amount": money_amount(order, "totalRefundedSet"),
        "currency_code": total_money["currencyCode"],
        "shipping_name": empty_to_none(shipping.get("name")),
        "shipping_company": empty_to_none(shipping.get("company")),
        "shipping_phone": empty_to_none(shipping.get("phone")),
        "shipping_address1": empty_to_none(shipping.get("address1")),
        "shipping_address2": empty_to_none(shipping.get("address2")),
        "shipping_city": empty_to_none(shipping.get("city")),
        "shipping_province": empty_to_none(shipping.get("province")),
        "shipping_province_code": empty_to_none(shipping.get("provinceCode")),
        "shipping_postal_code": empty_to_none(shipping.get("zip")),
        "shipping_country": empty_to_none(shipping.get("country")),
        "shipping_country_code": empty_to_none(shipping.get("countryCodeV2")),
        "shipping_latitude": shipping.get("latitude"),
        "shipping_longitude": shipping.get("longitude"),
        "billing_name": empty_to_none(billing.get("name")),
        "billing_company": empty_to_none(billing.get("company")),
        "billing_phone": empty_to_none(billing.get("phone")),
        "billing_address1": empty_to_none(billing.get("address1")),
        "billing_address2": empty_to_none(billing.get("address2")),
        "billing_city": empty_to_none(billing.get("city")),
        "billing_province": empty_to_none(billing.get("province")),
        "billing_province_code": empty_to_none(billing.get("provinceCode")),
        "billing_postal_code": empty_to_none(billing.get("zip")),
        "billing_country": empty_to_none(billing.get("country")),
        "billing_country_code": empty_to_none(billing.get("countryCodeV2")),
        "created_at": parse_datetime(order["createdAt"]),
        "processed_at": parse_datetime(order.get("processedAt")),
        "updated_at": parse_datetime(order["updatedAt"]),
        "cancelled_at": parse_datetime(order.get("cancelledAt")),
        "closed_at": parse_datetime(order.get("closedAt")),
        "synced_at": synced_at,
    }


def money_amount(value, field):
    """``handler.py``'s ``money_amount``: one of ``value``'s money sets.

    ``value`` is the order node and ``field`` is the set's name, matching the
    Lambda's signature. Zero rather than ``None`` when the set or the amount is
    absent: the destination's money columns are non-nullable ``Decimal(18, 2)``,
    so ``None`` is an insert failure rather than a NULL. The Lambda made the
    same choice, which is why this is not "fixed" here.
    """
    money_set = value.get(field) or {}
    shop_money = money_set.get("shopMoney") or {}
    amount = shop_money.get("amount")
    return Decimal(str(amount)) if amount is not None else Decimal("0")


def parse_datetime(value):
    """``handler.py``'s ``parse_datetime``: ISO 8601 in, ``datetime`` or None out.

    An absent or empty value is ``None``, which is what makes it usable for the
    nullable ``processed_at``, ``cancelled_at`` and ``closed_at`` as well as the
    required timestamps.

    The result is a ``datetime`` rather than a string because that is what the
    Lambda produces; the client decides how it crosses the wire.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def empty_to_none(value):
    """``handler.py``'s ``empty_to_none``: an empty string becomes NULL.

    Only a non-empty ``str`` survives, so ``None`` and any other type fall
    through to NULL rather than being stringified.
    """
    return value if isinstance(value, str) and value else None
