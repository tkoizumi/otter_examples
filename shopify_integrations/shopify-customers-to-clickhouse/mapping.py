"""How a Shopify customer maps onto Castor's ``shopify_customers`` row.

**This is the file to edit when the destination columns change.**

There is no deleted importer to transcribe here, which makes this mapping
different from the products and orders ones. Castor's import worker never
implemented customers: it had queries for products, variants and orders, and the
application never offered customers as a resource at all. So this is written
against the live Admin API's ``Customer`` type, and the field choices follow
what that worker did for the customer fields it *did* carry on an order --
``defaultEmailAddress.emailAddress`` and ``defaultPhoneNumber.phoneNumber``.

That last point is not cosmetic. ``Customer.email`` and ``Customer.phone`` are
still accepted by API 2026-07 and still return values, but they no longer appear
in the type's introspection at all, which is what a removed field looks like
before it stops answering. The replacements are what the order mapping already
reads, so the two jobs agree about which address is the customer's.

Nullability comes from the schema rather than from what a sample happened to
return. ``firstName``, ``lastName``, ``email`` and ``phone`` are nullable there
and the destination columns are nullable to match: a customer with no phone
number is ordinary, and a run that fails on one is worse than a NULL.
"""

from dataclasses import dataclass
from datetime import datetime

__all__ = ["CUSTOMER_COLUMNS", "Tenancy", "customer_row", "empty_to_none", "parse_datetime"]


@dataclass(frozen=True)
class Tenancy:
    """The three Castor ids that identify *which* dataset a row belongs to.

    One job instance syncs one dataset, so these are configuration rather than
    per-record data -- which is exactly why they can be wrong without anything
    failing. They come from Castor's control database: the workspace the
    connection lives in, the connection, and the dataset being filled.
    """

    workspace_id: str
    connection_id: str
    dataset_id: str


#: The destination columns, in schema order.
CUSTOMER_COLUMNS = (
    "workspace_id",
    "connection_id",
    "dataset_id",
    "shopify_customer_id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "note",
    "tags",
    "tax_exempt",
    "state",
    "created_at",
    "updated_at",
    "synced_at",
)


def customer_row(customer, synced_at, tenancy):
    """One ``shopify_customers`` row from a Shopify customer node.

    ``id``, ``tags``, ``taxExempt``, ``state``, ``createdAt`` and ``updatedAt``
    are indexed directly because the schema marks them non-null: a customer
    without one is a broken response, and failing the run names the field where
    a defaulted value would not. Everything nullable goes through
    ``empty_to_none``, so an absent value lands as NULL rather than as an empty
    string that reads like a real one.
    """
    email = (customer.get("defaultEmailAddress") or {}).get("emailAddress")
    phone = (customer.get("defaultPhoneNumber") or {}).get("phoneNumber")
    return {
        "workspace_id": tenancy.workspace_id,
        "connection_id": tenancy.connection_id,
        "dataset_id": tenancy.dataset_id,
        "shopify_customer_id": customer["id"],
        "first_name": empty_to_none(customer.get("firstName")),
        "last_name": empty_to_none(customer.get("lastName")),
        "email": empty_to_none(email),
        "phone": empty_to_none(phone),
        "note": empty_to_none(customer.get("note")),
        "tags": customer["tags"],
        "tax_exempt": customer["taxExempt"],
        "state": customer["state"],
        "created_at": parse_datetime(customer["createdAt"]),
        "updated_at": parse_datetime(customer["updatedAt"]),
        "synced_at": synced_at,
    }


def parse_datetime(value):
    """ISO 8601 in, ``datetime`` or None out.

    An absent or empty value is ``None``, which is what makes it usable for the
    nullable timestamps as well as the required ones. The result is a
    ``datetime`` rather than a string because the wire format is the client's
    business, not this module's.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def empty_to_none(value):
    """An empty string becomes NULL; anything that is not a non-empty string does.

    Only a non-empty ``str`` survives, so ``None`` and any other type fall
    through to NULL rather than being stringified.
    """
    return value if isinstance(value, str) and value else None
