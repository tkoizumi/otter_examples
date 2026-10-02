"""What this integration reads from Shopify.

The query lives in ``queries/customers.graphql`` as a document rather than as a
string assembled here, so an editor understands it. ``CUSTOMER_FIELDS`` below is
the contract between the document and the mapping: every field the mapping reads
is named there, and ``tests/test_source.py`` asserts that the query selects each
one. A field dropped from the document is otherwise a column that silently
arrives empty. That check is a shape check, not schema validation -- there is no
pulled Shopify schema in this directory, so a field that exists in neither place
is still possible, and the first run against the live API is what proves the
document.

**Customers is a flat connection, like orders and unlike products.** There is no
nested connection to walk and one destination table, so a page of customers is a
page of customers and nothing has to be attached to a node before it can be
mapped. What customers does *not* add is orders' filter trap: the ``orders``
connection returns *open* orders only unless the search string says otherwise,
which is why that job needs ``status:any``. The ``customers`` connection has no
such default, so the window clause below is the whole filter. See
``updated_since``.

The root is ``customers`` and the watermark compares ``Customer.updatedAt``,
which is what makes a run incremental rather than a scan of the whole customer
list.

Two of the fields below are nested -- ``defaultEmailAddress.emailAddress`` and
``defaultPhoneNumber.phoneNumber`` -- because ``Customer.email`` and
``Customer.phone`` no longer appear in the type's introspection. They sit in the
same tuple as the top-level fields for the same reason the orders job names its
address fields there: the mapping reads both, and a nested field dropped from the
document arrives as an empty column just as silently.
"""

from pathlib import Path

from otter_connectors.timeutil import to_iso

__all__ = [
    "CUSTOMERS_QUERY",
    "CUSTOMER_FIELDS",
    "QUERIES",
    "SORT_KEY",
    "fetch_page",
    "updated_since",
]

#: Every customer field the mapping reads, and therefore every field
#: ``customers.graphql`` must select. Named here rather than only inside the
#: document so a test can hold the two together. Nested fields sit in the same
#: tuple as the top-level ones because the mapping reads both: a field dropped
#: from an address block arrives as an empty column just as silently as one
#: dropped from the customer itself.
CUSTOMER_FIELDS = (
    "id",
    "firstName",
    "lastName",
    "defaultEmailAddress",
    "emailAddress",
    "defaultPhoneNumber",
    "phoneNumber",
    "note",
    "tags",
    "taxExempt",
    "state",
    "createdAt",
    "updatedAt",
)

#: The query document, resolved next to this module so the integration works
#: whatever directory the runner starts it from.
QUERIES = Path(__file__).parent / "queries"

CUSTOMERS_QUERY = (QUERIES / "customers.graphql").read_text(encoding="utf-8")

#: ``CustomerSortKeys.UPDATED_AT``. Type checking does not cover a variable's
#: *value*, so the member name is named once here and asserted in tests -- the
#: sibling integration once shipped a sort key Shopify rejected, and only a run
#: against the live API found out.
SORT_KEY = "UPDATED_AT"


def updated_since(window_start):
    """Shopify's search filter for "customers changed at or after this instant".

    One clause, and that is the whole difference from the orders job.
    ``updated_at`` is what makes the sync incremental rather than a scan of the
    whole customer list. There is deliberately no ``status:any`` here: it exists
    in the orders filter because that connection silently defaults to *open*
    orders, and the ``customers`` connection has no such default. Copying the
    clause across would be cargo cult. It was checked against the live API rather
    than assumed -- ``customersCount`` with no filter equals the count returned
    by an unfiltered walk, so nothing is hidden from this query.

    Shopify does *not* validate the field name here either. An unknown field
    matches everything rather than erroring, which would quietly turn an
    incremental sync into a full rescan every five minutes; that was verified
    live too, where a deliberately bogus field returned every record. A bad
    *value* fails the other way, by narrowing. Neither is an error, so
    ``tests/test_source.py`` asserts the exact shape instead of trusting Shopify
    to complain.
    """
    return "updated_at:>'%s'" % to_iso(window_start)


def fetch_page(shopify, window_start, page_size, cursor=None):
    """One page of customers changed since ``window_start``.

    Returns ``(customers, page_info)``. ``page_info["endCursor"]`` is what the
    caller checkpoints, so an interrupted run resumes mid-window instead of
    starting the window over.

    ``query`` and ``sortKey`` are variables rather than literals in the document,
    so the window and the sort are decided here -- one place to read when a run
    fetches the wrong customers.
    """
    variables = {
        "first": page_size,
        "after": cursor,
        "query": updated_since(window_start),
        "sortKey": SORT_KEY,
    }
    return shopify.connection(CUSTOMERS_QUERY, variables, path="customers")
