"""How a Shopify product and its variants map onto Castor's ClickHouse rows.

**This is a transcription of ``castor-app/infra/lambda/import-worker/handler.py``**
-- specifically its ``product_row``, ``variant_row`` and the three small
transforms beside them. That is deliberate rather than incidental: the Otter job
and the Lambda write the same two tables, so if the two disagreed about a value,
the destination would hold whichever ran last. Field order, nullable handling and
the money type are all copied from there, and the tests assert them.

The consequence is that this file is *not* idiomatic Python for a mapping. It
indexes dictionaries directly where the Lambda does, so a field Shopify stops
returning raises ``KeyError`` rather than quietly becoming an empty string -- a
schema change should fail a run, not write blank rows. It uses ``.get()`` exactly
where the Lambda does, for the fields Shopify marks nullable.

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
    "PRODUCT_COLUMNS",
    "VARIANT_COLUMNS",
    "Tenancy",
    "empty_to_none",
    "parse_datetime",
    "product_row",
    "variant_row",
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
PRODUCT_COLUMNS = (
    "workspace_id",
    "connection_id",
    "dataset_id",
    "shopify_product_id",
    "handle",
    "title",
    "description_html",
    "vendor",
    "product_type",
    "status",
    "tags",
    "created_at",
    "updated_at",
    "published_at",
    "synced_at",
)

VARIANT_COLUMNS = (
    "workspace_id",
    "connection_id",
    "dataset_id",
    "shopify_variant_id",
    "shopify_product_id",
    "title",
    "sku",
    "barcode",
    "price",
    "compare_at_price",
    "inventory_quantity",
    "inventory_policy",
    "taxable",
    "created_at",
    "updated_at",
    "synced_at",
)


def product_row(product, synced_at, tenancy):
    """One ``shopify_products`` row.

    ``tags`` is passed through untouched, empty strings included. Filtering them
    would be a silent divergence from the Lambda for a store that has one.
    """
    return {
        "workspace_id": tenancy.workspace_id,
        "connection_id": tenancy.connection_id,
        "dataset_id": tenancy.dataset_id,
        "shopify_product_id": product["id"],
        "handle": product["handle"],
        "title": product["title"],
        "description_html": product["descriptionHtml"],
        "vendor": product["vendor"],
        "product_type": product["productType"],
        "status": product["status"],
        "tags": product["tags"],
        "created_at": parse_datetime(product["createdAt"]),
        "updated_at": parse_datetime(product["updatedAt"]),
        "published_at": parse_datetime(product.get("publishedAt")),
        "synced_at": synced_at,
    }


def variant_row(variant, synced_at, tenancy):
    """One ``shopify_product_variants`` row.

    ``variant["product"]["id"]`` is a direct index, matching the Lambda: the
    caller attaches the parent product, so a variant without one is a bug here
    rather than something to tolerate.
    """
    return {
        "workspace_id": tenancy.workspace_id,
        "connection_id": tenancy.connection_id,
        "dataset_id": tenancy.dataset_id,
        "shopify_variant_id": variant["id"],
        "shopify_product_id": variant["product"]["id"],
        "title": variant["title"],
        "sku": empty_to_none(variant.get("sku")),
        "barcode": empty_to_none(variant.get("barcode")),
        "price": Decimal(variant["price"]),
        "compare_at_price": decimal_or_none(variant.get("compareAtPrice")),
        "inventory_quantity": variant.get("inventoryQuantity"),
        "inventory_policy": variant["inventoryPolicy"],
        "taxable": variant["taxable"],
        "created_at": parse_datetime(variant["createdAt"]),
        "updated_at": parse_datetime(variant["updatedAt"]),
        "synced_at": synced_at,
    }


def parse_datetime(value):
    """``handler.py``'s ``parse_datetime``: ISO 8601 in, ``datetime`` or None out.

    An absent or empty value is ``None``, which is what makes it usable for the
    nullable ``published_at`` as well as the required timestamps.

    The result is a ``datetime`` rather than a string because that is what the
    Lambda produces; the client decides how it crosses the wire.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def decimal_or_none(value):
    """``handler.py``'s ``decimal_or_none``: a nullable money column."""
    return Decimal(value) if value is not None else None


def empty_to_none(value):
    """``handler.py``'s ``empty_to_none``: an empty string becomes NULL.

    Only a non-empty ``str`` survives, so ``None`` and any other type fall
    through to NULL rather than being stringified.
    """
    return value if isinstance(value, str) and value else None
