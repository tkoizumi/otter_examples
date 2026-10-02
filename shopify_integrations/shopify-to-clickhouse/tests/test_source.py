"""Tests for the query documents and the window filter.

The queries are artifacts, so they can be checked without a store. The check that
earns its place is the one holding ``source.PRODUCT_FIELDS`` and
``source.VARIANT_FIELDS`` to the documents: a field the mapping reads but the
query does not select arrives as an empty column and syncs silently wrong.

It is a shape check, not schema validation. There is no pulled Shopify schema in
this directory, so a field named in neither the query nor the field list is still
possible, and the first run against the live API is what proves the document.
"""

import os
import sys
import unittest

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

import source  # noqa: E402
from otter_connectors.timeutil import parse_iso  # noqa: E402


class QueryDocumentTest(unittest.TestCase):
    def test_the_documents_load(self):
        self.assertIn("query Products", source.PRODUCTS_QUERY)
        self.assertIn("query MoreVariants", source.VARIANTS_QUERY)

    def test_products_selects_every_field_the_mapping_reads(self):
        for field in source.PRODUCT_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, source.PRODUCTS_QUERY)

    def test_the_nested_variant_page_selects_every_variant_field(self):
        # The nested connection is where most variants arrive, so a field missing
        # here would be missing for every product with fewer than one page.
        nested = source.PRODUCTS_QUERY.split("variants(first:", 1)[1]
        for field in source.VARIANT_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, nested)

    def test_the_remainder_query_selects_the_same_variant_fields(self):
        # Otherwise a product with more than one nested page of variants would
        # sync differently from a small one.
        for field in source.VARIANT_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, source.VARIANTS_QUERY)

    def test_both_documents_select_page_info(self):
        # Without pageInfo the caller cannot tell a complete page from a
        # truncated one, which is the truncation bug this arrangement prevents.
        for document in (source.PRODUCTS_QUERY, source.VARIANTS_QUERY):
            with self.subTest(document=document.splitlines()[0]):
                self.assertIn("hasNextPage", document)
                self.assertIn("endCursor", document)

    def test_the_nested_variant_page_matches_the_fetch_size(self):
        # The nested `first:` and MAX_VARIANTS_PER_PAGE are the same number on
        # purpose: that size is exactly when the remainder query is needed.
        self.assertIn(
            "variants(first: %d)" % source.MAX_VARIANTS_PER_PAGE, source.PRODUCTS_QUERY
        )


class WindowFilterTest(unittest.TestCase):
    def test_the_filter_is_the_documented_shape(self):
        # Shopify does not validate the field name here: an unknown one matches
        # everything rather than erroring, which would turn an incremental sync
        # into a full rescan every tick. So the shape is asserted rather than
        # trusted to complain.
        start = parse_iso("2026-02-01T00:00:00Z")
        self.assertEqual(source.updated_since(start), "updated_at:>'2026-02-01T00:00:00Z'")

    def test_the_sort_key_is_one_shopify_accepts(self):
        # A variable's *value* is not type-checked, so this is a string a run
        # would only discover was wrong against the live API.
        self.assertEqual(source.SORT_KEY, "UPDATED_AT")


if __name__ == "__main__":
    unittest.main()
