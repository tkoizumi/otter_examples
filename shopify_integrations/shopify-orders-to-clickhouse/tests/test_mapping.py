"""Tests for ``mapping.py``: the rows this integration writes.

These are less "does the mapping work" than "does the mapping still agree with
the Lambda". The two writers share a destination table, so a divergence is not a
broken test -- it is rows that change depending on which writer ran last. Each
test names the ``handler.py`` behaviour it pins.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION_DIR = os.path.dirname(HERE)
# The shared connectors live beside this job's directory, under
# shopify_integrations/lib/python. Resolving them relative to this file is what
# lets the suite run from anywhere -- a bare `python3 -m unittest discover
# -s tests`, an editor, or CI -- with no PYTHONPATH set by the caller.
LIB_PYTHON = os.path.join(os.path.dirname(INTEGRATION_DIR), "lib", "python")

for path in (LIB_PYTHON, INTEGRATION_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from mapping import (  # noqa: E402
    ORDER_COLUMNS,
    Tenancy,
    empty_to_none,
    money_amount,
    order_row,
    parse_datetime,
)

SYNCED_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: The three ids every row leads with, using the workspace Castor's stack pins.
TENANCY = Tenancy(
    workspace_id="00000000-0000-0000-0000-000000000001",
    connection_id="22222222-2222-2222-2222-222222222222",
    dataset_id="33333333-3333-3333-3333-333333333333",
)

#: An order node shaped like the GraphQL response. ``tags`` carries an empty
#: string, ``phone`` is blank and ``cancelledAt`` is absent on purpose -- see the
#: pass-through, empty-to-None and derived-flag tests.
ORDER = {
    "id": "gid://shopify/Order/5001",
    "name": "#1001",
    "email": "ada@example.com",
    "phone": "",
    "tags": ["vip", "", "wholesale"],
    "test": False,
    "createdAt": "2026-01-02T03:04:05Z",
    "processedAt": "2026-01-02T03:05:06Z",
    "updatedAt": "2026-02-03T04:05:06Z",
    "cancelledAt": None,
    "closedAt": None,
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "UNFULFILLED",
    "currencyCode": "USD",
    "customer": {
        "id": "gid://shopify/Customer/7001",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "defaultEmailAddress": {"emailAddress": "ada@example.com"},
        "defaultPhoneNumber": {"phoneNumber": "+15550100"},
    },
    "currentSubtotalPriceSet": {"shopMoney": {"amount": "19.99"}},
    "currentTotalDiscountsSet": {"shopMoney": {"amount": "2.00"}},
    "currentShippingPriceSet": {"shopMoney": {"amount": "5.00"}},
    "currentTotalTaxSet": {"shopMoney": {"amount": "1.60"}},
    "currentTotalPriceSet": {"shopMoney": {"amount": "24.59", "currencyCode": "USD"}},
    "totalRefundedSet": {"shopMoney": {"amount": "0.00"}},
    "shippingAddress": {
        "name": "Ada Lovelace",
        "company": "",
        "phone": "+15550100",
        "address1": "1 Analytical Engine Way",
        "address2": None,
        "city": "London",
        "province": "England",
        "provinceCode": "ENG",
        "zip": "EC1A",
        "country": "United Kingdom",
        "countryCodeV2": "GB",
        "latitude": 51.5072,
        "longitude": -0.1276,
    },
    "billingAddress": {
        "name": "Ada Lovelace",
        "company": None,
        "phone": "",
        "address1": "1 Analytical Engine Way",
        "address2": None,
        "city": "London",
        "province": "England",
        "provinceCode": "ENG",
        "zip": "EC1A",
        "country": "United Kingdom",
        "countryCodeV2": "GB",
    },
}


def order_fixture(**overrides):
    """The fixture order with top-level fields replaced or removed."""
    fixture = dict(ORDER)
    for key, value in overrides.items():
        fixture[key] = value
    return fixture


class OrderRowTest(unittest.TestCase):
    def test_the_row_has_exactly_the_destination_columns_in_order(self):
        # The order is the table's column order, so a key that moved would still
        # "have the right columns" under a set comparison. This is the test that
        # pins both.
        self.assertEqual(len(ORDER_COLUMNS), 54)
        self.assertEqual(list(order_row(ORDER, SYNCED_AT, TENANCY)), list(ORDER_COLUMNS))

    def test_the_identity_fields_are_copied_through(self):
        # handler.py indexes these directly; there is no transform to disagree
        # about.
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["shopify_order_id"], "gid://shopify/Order/5001")
        self.assertEqual(row["order_name"], "#1001")
        self.assertEqual(row["order_email"], "ada@example.com")
        self.assertEqual(row["financial_status"], "PAID")
        self.assertEqual(row["fulfillment_status"], "UNFULFILLED")
        self.assertEqual(row["currency_code"], "USD")

    def test_tags_pass_through_untouched(self):
        # The Lambda hands the GraphQL list straight to the insert, so an empty
        # tag survives. A filter here would be a silent divergence for any store
        # holding one.
        self.assertEqual(order_row(ORDER, SYNCED_AT, TENANCY)["tags"], ["vip", "", "wholesale"])

    def test_the_test_flag_is_the_boolean_shopify_sent(self):
        self.assertIs(order_row(ORDER, SYNCED_AT, TENANCY)["test"], False)
        self.assertIs(order_row(order_fixture(test=True), SYNCED_AT, TENANCY)["test"], True)

    def test_the_customer_block_is_read(self):
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["shopify_customer_id"], "gid://shopify/Customer/7001")
        self.assertEqual(row["customer_first_name"], "Ada")
        self.assertEqual(row["customer_last_name"], "Lovelace")
        self.assertEqual(row["customer_email"], "ada@example.com")
        self.assertEqual(row["customer_phone"], "+15550100")

    def test_an_order_without_a_customer_is_tolerated(self):
        # A guest checkout has no customer node at all, and the Lambda's `or {}`
        # is what makes that a row of NULLs rather than an AttributeError.
        row = order_row(order_fixture(customer=None), SYNCED_AT, TENANCY)
        self.assertIsNone(row["shopify_customer_id"])
        self.assertIsNone(row["customer_first_name"])
        self.assertIsNone(row["customer_email"])
        self.assertIsNone(row["customer_phone"])

    def test_optional_strings_become_none_not_empty(self):
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertIsNone(row["order_phone"])
        self.assertIsNone(row["shipping_company"])
        self.assertIsNone(row["shipping_address2"])
        self.assertIsNone(row["billing_company"])
        self.assertIsNone(row["billing_phone"])
        self.assertIsNone(row["billing_address2"])

    def test_an_order_without_addresses_leaves_them_none(self):
        # The Lambda's `or {}` again: an order Shopify returns with no shipping
        # address is a row of NULLs, not an error.
        row = order_row(order_fixture(shippingAddress=None, billingAddress=None), SYNCED_AT, TENANCY)
        self.assertIsNone(row["shipping_name"])
        self.assertIsNone(row["shipping_city"])
        self.assertIsNone(row["billing_province_code"])
        self.assertIsNone(row["shipping_latitude"])

    def test_coordinates_pass_through_including_none(self):
        # latitude/longitude are nullable Float64 columns, so they are copied
        # without empty_to_none -- which would turn a real 0.0 into NULL.
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["shipping_latitude"], 51.5072)
        self.assertEqual(row["shipping_longitude"], -0.1276)
        without = order_fixture(shippingAddress={"city": "London"})
        self.assertIsNone(order_row(without, SYNCED_AT, TENANCY)["shipping_latitude"])

    def test_a_missing_required_field_raises(self):
        # handler.py writes order["name"], so a Shopify schema change is a failed
        # run rather than a row with a blank column. KeyError is faithful to
        # that.
        without = {key: value for key, value in ORDER.items() if key != "name"}
        with self.assertRaises(KeyError):
            order_row(without, SYNCED_AT, TENANCY)


class CancelledTest(unittest.TestCase):
    """``cancelled`` and ``cancelled_at`` are two columns, and both exist."""

    def test_an_uncancelled_order_is_false_with_no_timestamp(self):
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertIs(row["cancelled"], False)
        self.assertIsNone(row["cancelled_at"])

    def test_a_cancelled_order_sets_both_the_flag_and_the_timestamp(self):
        # The flag is derived from the timestamp, so it cannot disagree with it;
        # the timestamp is still carried because a report wants the instant.
        row = order_row(order_fixture(cancelledAt="2026-02-04T05:06:07Z"), SYNCED_AT, TENANCY)
        self.assertIs(row["cancelled"], True)
        self.assertEqual(row["cancelled_at"], datetime(2026, 2, 4, 5, 6, 7, tzinfo=timezone.utc))


class MoneyTest(unittest.TestCase):
    def test_money_is_a_decimal_not_a_float(self):
        # Decimal("19.99") carries the digits; 19.99 carries a binary
        # approximation. The difference only shows on values a float cannot hold,
        # which is exactly the kind of thing that goes unnoticed until it does.
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["subtotal_price"], Decimal("19.99"))
        self.assertEqual(row["total_price"], Decimal("24.59"))
        self.assertNotIsInstance(row["total_price"], float)

    def test_the_total_is_indexed_directly(self):
        # handler.py does Decimal(order["currentTotalPriceSet"]["shopMoney"]
        # ["amount"]); the currency comes from the same set.
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["total_price"], Decimal("24.59"))
        self.assertEqual(row["currency_code"], "USD")

    def test_a_missing_total_money_set_is_an_error_not_zero(self):
        # Only the *other* money sets tolerate absence. The total one is indexed,
        # so its disappearance is a schema change that should fail the run.
        without = {key: value for key, value in ORDER.items() if key != "currentTotalPriceSet"}
        with self.assertRaises(KeyError):
            order_row(without, SYNCED_AT, TENANCY)

    def test_an_absent_money_set_is_zero_not_none(self):
        # The columns are non-nullable Decimal(18,2), so None would fail the
        # insert. handler.py's money_amount chose 0 and this reproduces it.
        without = {
            key: value
            for key, value in ORDER.items()
            if key not in ("currentTotalDiscountsSet", "totalRefundedSet")
        }
        row = order_row(without, SYNCED_AT, TENANCY)
        self.assertEqual(row["discount_amount"], Decimal("0"))
        self.assertEqual(row["refunded_amount"], Decimal("0"))
        self.assertIsNotNone(row["discount_amount"])

    def test_money_amount_reads_the_shop_money_amount(self):
        self.assertEqual(
            money_amount(ORDER, "currentShippingPriceSet"), Decimal("5.00")
        )


class TenancyTest(unittest.TestCase):
    def test_the_row_leads_with_the_tenant_ids(self):
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(list(row)[:3], ["workspace_id", "connection_id", "dataset_id"])
        self.assertEqual(row["workspace_id"], TENANCY.workspace_id)
        self.assertEqual(row["connection_id"], TENANCY.connection_id)
        self.assertEqual(row["dataset_id"], TENANCY.dataset_id)


class TimestampTest(unittest.TestCase):
    def test_timestamps_are_datetimes_not_strings(self):
        # parse_datetime returns a datetime, as handler.py does. The wire format
        # is the client's business.
        row = order_row(ORDER, SYNCED_AT, TENANCY)
        self.assertEqual(row["created_at"], datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(row["processed_at"], datetime(2026, 1, 2, 3, 5, 6, tzinfo=timezone.utc))
        self.assertEqual(row["updated_at"], datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc))

    def test_the_nullable_timestamps_accept_absence(self):
        row = order_row(order_fixture(processedAt=None, closedAt=None), SYNCED_AT, TENANCY)
        self.assertIsNone(row["processed_at"])
        self.assertIsNone(row["closed_at"])

    def test_a_present_closed_at_is_parsed(self):
        row = order_row(order_fixture(closedAt="2026-03-04T05:06:07Z"), SYNCED_AT, TENANCY)
        self.assertEqual(row["closed_at"], datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc))

    def test_synced_at_is_the_argument(self):
        self.assertEqual(order_row(ORDER, SYNCED_AT, TENANCY)["synced_at"], SYNCED_AT)


class TransformTest(unittest.TestCase):
    """The helpers copied from handler.py, tested directly."""

    def test_parse_datetime_accepts_shopify_zulu_form(self):
        self.assertEqual(parse_datetime("2026-01-02T03:04:05Z"),
                         datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    def test_parse_datetime_treats_absent_and_empty_as_none(self):
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))

    def test_empty_to_none_only_keeps_a_non_empty_string(self):
        self.assertEqual(empty_to_none("ENG"), "ENG")
        self.assertIsNone(empty_to_none(""))
        self.assertIsNone(empty_to_none(None))
        self.assertIsNone(empty_to_none(0))


if __name__ == "__main__":
    unittest.main()
