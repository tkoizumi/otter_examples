"""What this integration reads from Shopify.

The query lives in ``queries/orders.graphql`` as a document rather than as a
string assembled here, so an editor understands it. ``ORDER_FIELDS`` below is
the contract between the document and the mapping: every field the mapping reads
is named there, and ``tests/test_source.py`` asserts that the query selects each
one. A field dropped from the document is otherwise a column that silently
arrives empty. That check is a shape check, not schema validation -- there is no
pulled Shopify schema in this directory, so a field that exists in neither place
is still possible, and the first run against the live API is what proves the
document.

**Orders is a flat connection, and that is the whole difference from the sibling
job.** There is no nested connection to walk and one destination table, so a
page of orders is a page of orders and nothing has to be attached to a node
before it can be mapped. What orders does add is a filter trap: the ``orders``
connection returns *open* orders only unless the search string says otherwise,
so closed, cancelled and archived orders have to be asked for explicitly. See
``updated_since``.

The root is ``orders`` and the watermark compares ``Order.updatedAt``, which is
what makes a run incremental rather than a scan of the whole order history.
"""

from pathlib import Path

from otter_connectors.timeutil import to_iso

__all__ = [
    "ORDERS_QUERY",
    "ORDER_FIELDS",
    "QUERIES",
    "SORT_KEY",
    "fetch_page",
    "updated_since",
]

#: Every order field the mapping reads, and therefore every field
#: ``orders.graphql`` must select. Named here rather than only inside the
#: document so a test can hold the two together. Nested fields sit in the same
#: tuple as the top-level ones because the mapping reads both: a field dropped
#: from an address block arrives as an empty column just as silently as one
#: dropped from the order itself.
ORDER_FIELDS = (
    "id",
    "name",
    "email",
    "phone",
    "tags",
    "test",
    "createdAt",
    "processedAt",
    "updatedAt",
    "cancelledAt",
    "closedAt",
    "displayFinancialStatus",
    "displayFulfillmentStatus",
    "customer",
    "firstName",
    "lastName",
    "defaultEmailAddress",
    "emailAddress",
    "defaultPhoneNumber",
    "phoneNumber",
    "currentSubtotalPriceSet",
    "currentTotalDiscountsSet",
    "currentShippingPriceSet",
    "currentTotalTaxSet",
    "currentTotalPriceSet",
    "totalRefundedSet",
    "shopMoney",
    "amount",
    "currencyCode",
    "shippingAddress",
    "company",
    "address1",
    "address2",
    "city",
    "province",
    "provinceCode",
    "zip",
    "country",
    "countryCodeV2",
    "latitude",
    "longitude",
    "billingAddress",
)

#: The query document, resolved next to this module so the integration works
#: whatever directory the runner starts it from.
QUERIES = Path(__file__).parent / "queries"

ORDERS_QUERY = (QUERIES / "orders.graphql").read_text(encoding="utf-8")

#: ``OrderSortKeys.UPDATED_AT``. Type checking does not cover a variable's
#: *value*, so the member name is named once here and asserted in tests -- the
#: sibling integration once shipped a sort key Shopify rejected, and only a run
#: against the live API found out.
SORT_KEY = "UPDATED_AT"


def updated_since(window_start):
    """Shopify's search filter for "orders changed at or after this instant".

    Two clauses, and both are load-bearing. ``updated_at`` is what makes the sync
    incremental rather than a scan of the whole order history. ``status:any`` is
    required because Shopify's ``orders`` connection returns **open** orders
    only: without it, closed, cancelled and archived orders silently vanish from
    the sync, and nothing fails to say so.

    Shopify does *not* validate the field name here either. An unknown field
    matches everything rather than erroring, which would quietly turn an
    incremental sync into a full rescan every five minutes; a bad *value* fails
    the other way, by narrowing. Neither is an error, so
    ``tests/test_source.py`` asserts the exact shape instead of trusting Shopify
    to complain.
    """
    return "status:any AND updated_at:>'%s'" % to_iso(window_start)


def fetch_page(shopify, window_start, page_size, cursor=None):
    """One page of orders changed since ``window_start``.

    Returns ``(orders, page_info)``. ``page_info["endCursor"]`` is what the
    caller checkpoints, so an interrupted run resumes mid-window instead of
    starting the window over.

    ``query`` and ``sortKey`` are variables rather than literals in the document,
    so the window and the sort are decided here -- one place to read when a run
    fetches the wrong orders.
    """
    variables = {
        "first": page_size,
        "after": cursor,
        "query": updated_since(window_start),
        "sortKey": SORT_KEY,
    }
    return shopify.connection(ORDERS_QUERY, variables, path="orders")
