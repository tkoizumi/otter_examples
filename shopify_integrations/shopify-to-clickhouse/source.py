"""What this integration reads from Shopify.

The queries live in ``queries/*.graphql`` as documents rather than as strings
assembled here, so an editor understands them. ``PRODUCT_FIELDS`` and
``VARIANT_FIELDS`` below are the contract between the document and the mapping:
every field the mapping reads is named there, and ``tests/test_source.py``
asserts that the query selects each one. A field dropped from the document is
otherwise a column that silently arrives empty. That check is a shape check, not
schema validation -- there is no pulled Shopify schema in this directory, so a
field that exists in neither place is still possible, and the first run against
the live API is what proves the document.

**Products, not variants, is the root.** ``productVariants`` is the flatter
connection and the obvious choice, but a product-level change does not bump its
variants' ``updatedAt``. A variant-level watermark would therefore never re-sync
a renamed product, and the product row carries the fields most likely to be
renamed. So the root is ``products`` and the watermark compares
``Product.updatedAt``.

The cost is a nested connection. ``variants`` inside a product is capped by
Shopify, so ``pageInfo`` is selected and ``all_variants`` fetches the remainder
through ``product-variants.graphql``. Silently syncing only the first nested
page of a large product is the failure mode that arrangement exists to prevent.

The mapping is written against a variant, so ``variants_with_product`` hands
each variant its parent product underneath it. That keeps the parent's fields
reachable from the variant row without a second lookup.
"""

from pathlib import Path

from otter_connectors.shopify import ShopifyError, numeric_id
from otter_connectors.timeutil import to_iso

__all__ = [
    "MAX_VARIANTS_PER_PAGE",
    "PRODUCTS_QUERY",
    "PRODUCT_FIELDS",
    "QUERIES",
    "SORT_KEY",
    "VARIANTS_QUERY",
    "VARIANT_FIELDS",
    "all_variants",
    "fetch_page",
    "fetch_variants",
    "updated_since",
    "variants_with_product",
]

#: Every product field the mapping reads, and therefore every field
#: `products.graphql` must select. Named here rather than only inside the
#: document so a test can hold the two together.
PRODUCT_FIELDS = (
    "id",
    "handle",
    "title",
    "descriptionHtml",
    "vendor",
    "productType",
    "status",
    "tags",
    "createdAt",
    "updatedAt",
    "publishedAt",
)

#: Every variant field the mapping reads. Selected both by the nested connection
#: in products.graphql and by product-variants.graphql, which fetches the
#: remainder -- so both documents must carry all of them, or a large product
#: would sync differently from a small one.
VARIANT_FIELDS = (
    "id",
    "title",
    "sku",
    "barcode",
    "price",
    "compareAtPrice",
    "inventoryQuantity",
    "inventoryPolicy",
    "taxable",
    "createdAt",
    "updatedAt",
)

#: Query documents, resolved next to this module so the integration works
#: whatever directory the runner starts it from.
QUERIES = Path(__file__).parent / "queries"

PRODUCTS_QUERY = (QUERIES / "products.graphql").read_text(encoding="utf-8")
VARIANTS_QUERY = (QUERIES / "product-variants.graphql").read_text(encoding="utf-8")

#: Shopify's ceiling for a `first:` argument, and the size of the nested
#: `variants` page in products.graphql. The two are the same number on purpose:
#: a product with more variants than one nested page is exactly the case the
#: remainder query exists for.
MAX_VARIANTS_PER_PAGE = 100

#: ``ProductSortKeys.UPDATED_AT``. Type checking does not cover a variable's
#: *value*, so the member name is named once here and asserted in tests -- the
#: sibling integration once shipped a sort key Shopify rejected, and only a run
#: against the live API found out.
SORT_KEY = "UPDATED_AT"


def updated_since(window_start):
    """Shopify's search filter for "changed at or after this instant".

    Shopify does *not* validate the field name here: an unknown field matches
    everything rather than erroring, which would quietly turn an incremental
    sync into a full rescan every five minutes. ``tests/test_source.py`` asserts
    the shape for that reason.
    """
    return "updated_at:>'%s'" % to_iso(window_start)


def fetch_page(shopify, window_start, page_size, cursor=None):
    """One page of products changed since ``window_start``.

    Returns ``(products, page_info)``. ``page_info["endCursor"]`` is what the
    caller checkpoints, so an interrupted run resumes mid-window instead of
    starting the window over.
    """
    variables = {
        "first": page_size,
        "after": cursor,
        "query": updated_since(window_start),
        "sortKey": SORT_KEY,
    }
    return shopify.connection(PRODUCTS_QUERY, variables, path="products")


def fetch_variants(shopify, product_id, cursor=None, page_size=MAX_VARIANTS_PER_PAGE):
    """One page of a single product's variants, continuing the nested connection.

    ``product_id`` is a GID; Shopify's filter wants the numeric tail.
    """
    variables = {
        "first": page_size,
        "after": cursor,
        "filter": "product_id:%s" % numeric_id(product_id),
    }
    return shopify.connection(VARIANTS_QUERY, variables, path="productVariants")


def all_variants(shopify, product, page_size=MAX_VARIANTS_PER_PAGE):
    """Every variant of ``product``, past the nested page when it is truncated.

    The nested page is Shopify's cheapest way to fetch variants, but it stops at
    ``page_size``. ``pageInfo.hasNextPage`` says whether it did, so the remainder
    is fetched explicitly rather than assumed away. A cursor that does not
    advance is an error, not a silent infinite loop.
    """
    connection = product.get("variants") or {}
    variants = list(connection.get("nodes") or [])
    page_info = connection.get("pageInfo") or {}
    cursor = page_info.get("endCursor")

    while page_info.get("hasNextPage"):
        more, page_info = fetch_variants(
            shopify, product.get("id"), cursor=cursor, page_size=page_size
        )
        if not more:
            raise ShopifyError(
                "Shopify reported more variants for %s but returned none" % product.get("id")
            )
        variants.extend(more)
        if page_info.get("endCursor") == cursor:
            raise ShopifyError(
                "Shopify variant paging did not advance for %s" % product.get("id")
            )
        cursor = page_info.get("endCursor")

    return variants


def variants_with_product(shopify, product):
    """Every variant of ``product``, each carrying its parent under ``product``.

    The parent is the product's own top-level fields with ``variants`` removed.
    That connection is what this function is walking, so keeping it would make
    the document self-referential -- product, then variants, then a variant, then
    the product again -- which a variant serialized to a log line cannot survive.
    Nothing reads ``variants`` from the parent.
    """
    parent = {key: value for key, value in product.items() if key != "variants"}
    for variant in all_variants(shopify, product):
        variant["product"] = parent
        yield variant
