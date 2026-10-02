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
    PRODUCT_COLUMNS,
    VARIANT_COLUMNS,
    Tenancy,
    empty_to_none,
    parse_datetime,
    product_row,
    variant_row,
)

SYNCED_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: The three ids every row leads with, using the workspace Castor's stack pins.
TENANCY = Tenancy(
    workspace_id="00000000-0000-0000-0000-000000000001",
    connection_id="22222222-2222-2222-2222-222222222222",
    dataset_id="33333333-3333-3333-3333-333333333333",
)

#: A product node shaped like the GraphQL response, with one variant beneath it.
#: ``tags`` carries an empty string on purpose -- see the pass-through test.
PRODUCT = {
    "id": "gid://shopify/Product/1001",
    "handle": "trail-mug",
    "title": "Trail Mug",
    "descriptionHtml": "<p>Enamel.</p>",
    "vendor": "Castor",
    "productType": "Drinkware",
    "status": "ACTIVE",
    "tags": ["camp", "", "mug"],
    "createdAt": "2026-01-02T03:04:05Z",
    "updatedAt": "2026-02-03T04:05:06Z",
    "publishedAt": None,
    "variants": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [
            {
                "id": "gid://shopify/ProductVariant/2001",
                "title": "Default Title",
                "sku": "MUG-1",
                "barcode": None,
                "price": "19.99",
                "compareAtPrice": "24.99",
                "inventoryQuantity": 7,
                "inventoryPolicy": "DENY",
                "taxable": True,
                "createdAt": "2026-01-02T03:04:05Z",
                "updatedAt": "2026-02-03T04:05:06Z",
            }
        ],
    },
}


def variant_fixture(**overrides):
    """The product's single variant, carrying its parent as ``source`` does."""
    variant = dict(PRODUCT["variants"]["nodes"][0])
    variant["product"] = {key: value for key, value in PRODUCT.items() if key != "variants"}
    variant.update(overrides)
    return variant


class ProductRowTest(unittest.TestCase):
    def test_the_row_has_exactly_the_destination_columns(self):
        self.assertEqual(list(product_row(PRODUCT, SYNCED_AT, TENANCY)), list(PRODUCT_COLUMNS))

    def test_the_vendor_fields_are_copied_through(self):
        # handler.py indexes these directly; there is no transform to disagree about.
        row = product_row(PRODUCT, SYNCED_AT, TENANCY)
        self.assertEqual(row["shopify_product_id"], "gid://shopify/Product/1001")
        self.assertEqual(row["handle"], "trail-mug")
        self.assertEqual(row["description_html"], "<p>Enamel.</p>")
        self.assertEqual(row["product_type"], "Drinkware")
        self.assertEqual(row["status"], "ACTIVE")

    def test_tags_pass_through_untouched(self):
        # The Lambda hands the GraphQL list straight to the insert, so an empty
        # tag survives. This mapping did filter blanks once; that was a silent
        # divergence for any store holding one.
        self.assertEqual(product_row(PRODUCT, SYNCED_AT, TENANCY)["tags"], ["camp", "", "mug"])

    def test_timestamps_are_datetimes_not_strings(self):
        # parse_datetime returns a datetime, as handler.py does. The wire format
        # is the client's business.
        row = product_row(PRODUCT, SYNCED_AT, TENANCY)
        self.assertEqual(row["created_at"], datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(row["updated_at"], datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc))

    def test_an_absent_published_at_is_none(self):
        self.assertIsNone(product_row(PRODUCT, SYNCED_AT, TENANCY)["published_at"])

    def test_a_present_published_at_is_parsed(self):
        row = product_row({**PRODUCT, "publishedAt": "2026-03-04T05:06:07Z"}, SYNCED_AT, TENANCY)
        self.assertEqual(row["published_at"], datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc))

    def test_a_missing_required_field_raises(self):
        # handler.py writes product["handle"], so a Shopify schema change is a
        # failed run rather than a row with a blank column. KeyError is the
        # faithful reproduction of that.
        without = {key: value for key, value in PRODUCT.items() if key != "handle"}
        with self.assertRaises(KeyError):
            product_row(without, SYNCED_AT, TENANCY)


class VariantRowTest(unittest.TestCase):
    def test_the_row_has_exactly_the_destination_columns(self):
        self.assertEqual(
            list(variant_row(variant_fixture(), SYNCED_AT, TENANCY)), list(VARIANT_COLUMNS)
        )

    def test_the_parent_product_is_indexed_directly(self):
        self.assertEqual(
            variant_row(variant_fixture(), SYNCED_AT, TENANCY)["shopify_product_id"],
            "gid://shopify/Product/1001",
        )

    def test_a_missing_parent_is_an_error(self):
        # The caller attaches it; a variant without one is a bug in source.py,
        # not something to tolerate here.
        variant = variant_fixture()
        del variant["product"]
        with self.assertRaises(KeyError):
            variant_row(variant, SYNCED_AT, TENANCY)

    def test_money_is_a_decimal_not_a_float(self):
        # Decimal("19.99") carries the digits; 19.99 carries a binary
        # approximation. The difference only shows on values a float cannot hold,
        # which is exactly the kind of thing that goes unnoticed until it does.
        row = variant_row(variant_fixture(), SYNCED_AT, TENANCY)
        self.assertEqual(row["price"], Decimal("19.99"))
        self.assertEqual(row["compare_at_price"], Decimal("24.99"))
        self.assertNotIsInstance(row["price"], float)

    def test_a_nullable_price_is_none(self):
        row = variant_row(variant_fixture(compareAtPrice=None), SYNCED_AT, TENANCY)
        self.assertIsNone(row["compare_at_price"])

    def test_an_empty_string_price_is_not_silently_zero(self):
        # handler.py does Decimal(variant["price"]) with no guard, so an empty
        # price raises rather than becoming 0.00.
        with self.assertRaises(Exception):
            variant_row(variant_fixture(price=""), SYNCED_AT, TENANCY)

    def test_optional_strings_become_none_not_empty(self):
        row = variant_row(variant_fixture(sku="", barcode=None), SYNCED_AT, TENANCY)
        self.assertIsNone(row["sku"])
        self.assertIsNone(row["barcode"])

    def test_inventory_quantity_passes_through_including_none(self):
        self.assertEqual(variant_row(variant_fixture(), SYNCED_AT, TENANCY)["inventory_quantity"], 7)
        self.assertIsNone(
            variant_row(variant_fixture(inventoryQuantity=None), SYNCED_AT, TENANCY)[
                "inventory_quantity"
            ]
        )

    def test_taxable_is_the_boolean_shopify_sent(self):
        self.assertIs(variant_row(variant_fixture(taxable=False), SYNCED_AT, TENANCY)["taxable"], False)


class TenancyTest(unittest.TestCase):
    def test_both_rows_lead_with_the_tenant_ids(self):
        rows = (product_row(PRODUCT, SYNCED_AT, TENANCY),
                variant_row(variant_fixture(), SYNCED_AT, TENANCY))
        for row in rows:
            with self.subTest(columns=list(row)[:3]):
                self.assertEqual(list(row)[:3], ["workspace_id", "connection_id", "dataset_id"])
                self.assertEqual(row["workspace_id"], TENANCY.workspace_id)
                self.assertEqual(row["connection_id"], TENANCY.connection_id)
                self.assertEqual(row["dataset_id"], TENANCY.dataset_id)


class TransformTest(unittest.TestCase):
    """The three helpers copied from handler.py, tested directly."""

    def test_parse_datetime_accepts_shopify_zulu_form(self):
        self.assertEqual(parse_datetime("2026-01-02T03:04:05Z"),
                         datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    def test_parse_datetime_treats_absent_and_empty_as_none(self):
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))

    def test_empty_to_none_only_keeps_a_non_empty_string(self):
        self.assertEqual(empty_to_none("MUG-1"), "MUG-1")
        self.assertIsNone(empty_to_none(""))
        self.assertIsNone(empty_to_none(None))
        self.assertIsNone(empty_to_none(0))


if __name__ == "__main__":
    unittest.main()
