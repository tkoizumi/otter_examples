"""Tests for ``mapping.py``: the rows this integration writes.

These are less "does the mapping work" than "does the mapping still agree with
the destination". The orders and products siblings pin their mapping against
Castor's import worker, because they are transcriptions of it. There is no worker
to agree with here -- the worker never implemented customers and the application
never offered them as a resource -- so what these hold still is the contract the
live Admin API implies: the destination's column order, and which values are
nullable in the schema rather than merely absent from whatever sample was to
hand.

The removed-field case below is the one worth reading twice. ``Customer.email``
and ``Customer.phone`` are still accepted by the pinned API version and still
return values, but they no longer appear in the type's introspection. The mapping
reads the nested address fields instead, so a test that fed it the old field
names would pass while the columns went subtly wrong.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

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
    CUSTOMER_COLUMNS,
    Tenancy,
    customer_row,
    empty_to_none,
    parse_datetime,
)

SYNCED_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: The three ids every row leads with, using the workspace Castor's stack pins.
TENANCY = Tenancy(
    workspace_id="00000000-0000-0000-0000-000000000001",
    connection_id="22222222-2222-2222-2222-222222222222",
    dataset_id="33333333-3333-3333-3333-333333333333",
)

#: A customer node shaped like the GraphQL response. ``tags`` carries an empty
#: string, ``note`` is blank and both ``firstName`` and the default email address
#: are set on purpose -- see the pass-through, empty-to-None and nested-address
#: tests.
CUSTOMER = {
    "id": "gid://shopify/Customer/7001",
    "firstName": "Ada",
    "lastName": "Lovelace",
    "defaultEmailAddress": {"emailAddress": "ada@example.com"},
    "defaultPhoneNumber": {"phoneNumber": "+15550100"},
    "note": "",
    "tags": ["vip", "", "wholesale"],
    "taxExempt": False,
    "state": "ENABLED",
    "createdAt": "2026-01-02T03:04:05Z",
    "updatedAt": "2026-02-03T04:05:06Z",
}


def customer_fixture(**overrides):
    """The fixture customer with top-level fields replaced or removed."""
    fixture = dict(CUSTOMER)
    for key, value in overrides.items():
        fixture[key] = value
    return fixture


class CustomerRowTest(unittest.TestCase):
    def test_the_row_has_exactly_the_destination_columns_in_order(self):
        # The order is the table's column order, so a key that moved would still
        # "have the right columns" under a set comparison. This is the test that
        # pins both. Fifteen columns, and no money column among them: customers
        # carry no prices.
        self.assertEqual(len(CUSTOMER_COLUMNS), 15)
        self.assertEqual(list(customer_row(CUSTOMER, SYNCED_AT, TENANCY)), list(CUSTOMER_COLUMNS))

    def test_the_identity_fields_are_copied_through(self):
        # The mapping indexes these directly; there is no transform to disagree
        # about.
        row = customer_row(CUSTOMER, SYNCED_AT, TENANCY)
        self.assertEqual(row["shopify_customer_id"], "gid://shopify/Customer/7001")
        self.assertEqual(row["first_name"], "Ada")
        self.assertEqual(row["last_name"], "Lovelace")
        self.assertEqual(row["email"], "ada@example.com")
        self.assertEqual(row["phone"], "+15550100")
        self.assertEqual(row["state"], "ENABLED")

    def test_the_nested_address_fields_are_read(self):
        # The mapping reads defaultEmailAddress.emailAddress and
        # defaultPhoneNumber.phoneNumber, as the orders job does for its customer
        # block. Both are one level down, so a document that selected the blocks
        # but not their contents would empty these two columns.
        row = customer_row(CUSTOMER, SYNCED_AT, TENANCY)
        self.assertEqual(row["email"], "ada@example.com")
        self.assertEqual(row["phone"], "+15550100")

    def test_the_removed_top_level_email_and_phone_do_not_win(self):
        # Customer.email and Customer.phone still answer in the pinned API
        # version but are absent from the type's introspection -- what a removed
        # field looks like before it stops answering. A node carrying both must
        # map to the nested values, which are the ones the query actually
        # selects.
        row = customer_row(
            customer_fixture(email="old@example.com", phone="+15559999"),
            SYNCED_AT,
            TENANCY,
        )
        self.assertEqual(row["email"], "ada@example.com")
        self.assertEqual(row["phone"], "+15550100")

    def test_tags_pass_through_untouched(self):
        # The GraphQL list goes straight to the insert, so an empty tag
        # survives. A filter here would be a silent divergence for any store
        # holding one.
        self.assertEqual(
            customer_row(CUSTOMER, SYNCED_AT, TENANCY)["tags"], ["vip", "", "wholesale"]
        )

    def test_the_tax_exempt_flag_is_the_boolean_shopify_sent(self):
        self.assertIs(customer_row(CUSTOMER, SYNCED_AT, TENANCY)["tax_exempt"], False)
        self.assertIs(
            customer_row(customer_fixture(taxExempt=True), SYNCED_AT, TENANCY)["tax_exempt"], True
        )

    def test_optional_strings_become_none_not_empty(self):
        # A blank note is ordinary, and the column is nullable to match. An
        # empty string would read like a real value.
        row = customer_row(CUSTOMER, SYNCED_AT, TENANCY)
        self.assertIsNone(row["note"])

        blank = customer_fixture(
            firstName="", lastName="", defaultEmailAddress={"emailAddress": ""}
        )
        row = customer_row(blank, SYNCED_AT, TENANCY)
        self.assertIsNone(row["first_name"])
        self.assertIsNone(row["last_name"])
        self.assertIsNone(row["email"])

    def test_a_customer_without_a_default_address_leaves_it_none(self):
        # The mapping's `or {}` is what makes a customer with no default address
        # a row of NULLs rather than an AttributeError.
        row = customer_row(
            customer_fixture(defaultEmailAddress=None, defaultPhoneNumber=None),
            SYNCED_AT,
            TENANCY,
        )
        self.assertIsNone(row["email"])
        self.assertIsNone(row["phone"])

    def test_a_missing_required_field_raises(self):
        # The mapping indexes ``state`` directly because the schema marks it
        # non-null, so a Shopify schema change is a failed run rather than a row
        # with a blank column. KeyError is faithful to that.
        without = {key: value for key, value in CUSTOMER.items() if key != "state"}
        with self.assertRaises(KeyError):
            customer_row(without, SYNCED_AT, TENANCY)


class TenancyTest(unittest.TestCase):
    def test_the_row_leads_with_the_tenant_ids(self):
        row = customer_row(CUSTOMER, SYNCED_AT, TENANCY)
        self.assertEqual(list(row)[:3], ["workspace_id", "connection_id", "dataset_id"])
        self.assertEqual(row["workspace_id"], TENANCY.workspace_id)
        self.assertEqual(row["connection_id"], TENANCY.connection_id)
        self.assertEqual(row["dataset_id"], TENANCY.dataset_id)


class TimestampTest(unittest.TestCase):
    def test_timestamps_are_datetimes_not_strings(self):
        # parse_datetime returns a datetime. The wire format is the client's
        # business.
        row = customer_row(CUSTOMER, SYNCED_AT, TENANCY)
        self.assertEqual(row["created_at"], datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(row["updated_at"], datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc))

    def test_synced_at_is_the_argument(self):
        self.assertEqual(customer_row(CUSTOMER, SYNCED_AT, TENANCY)["synced_at"], SYNCED_AT)


class TransformTest(unittest.TestCase):
    """The helpers, tested directly."""

    def test_parse_datetime_accepts_shopify_zulu_form(self):
        self.assertEqual(parse_datetime("2026-01-02T03:04:05Z"),
                         datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    def test_parse_datetime_treats_absent_and_empty_as_none(self):
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))

    def test_empty_to_none_only_keeps_a_non_empty_string(self):
        self.assertEqual(empty_to_none("ENABLED"), "ENABLED")
        self.assertIsNone(empty_to_none(""))
        self.assertIsNone(empty_to_none(None))
        self.assertIsNone(empty_to_none(0))


if __name__ == "__main__":
    unittest.main()
