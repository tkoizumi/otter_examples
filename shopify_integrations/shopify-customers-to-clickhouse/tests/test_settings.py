"""Tests for ``settings.py``: the defaults, the required values, the errors.

The dataclass is what lets a test build a configuration without touching
``os.environ``; these tests cover the other half -- that ``load()`` reads the
environment the way ``otter.yaml`` says it does, and that a bad value fails here
rather than half way through a sync.

The table-name cases matter more than they look. The value is interpolated into
an INSERT, so an unvalidated name is not a style problem, it is the statement.
"""

import os
import sys
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

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

from otter_connectors.errors import ConfigError  # noqa: E402

from settings import Settings  # noqa: E402

#: The settings with no default, so every ``load()`` needs them.
REQUIRED = {
    "SHOPIFY_STORE": "example.myshopify.com",
    "CLICKHOUSE_URL": "https://clickhouse.example:8443",
    "CASTOR_WORKSPACE_ID": "11111111-1111-1111-1111-111111111111",
    "CASTOR_CONNECTION_ID": "22222222-2222-2222-2222-222222222222",
    "CASTOR_DATASET_ID": "33333333-3333-3333-3333-333333333333",
}


def load(**overrides):
    """``Settings.load()`` with ``overrides`` on top of the required values."""
    environ = dict(REQUIRED)
    environ.update(overrides)
    with mock.patch.dict(os.environ, environ, clear=True):
        return Settings.load()


class SettingsLoadTest(unittest.TestCase):
    def test_required_values_are_read(self):
        cfg = load()
        self.assertEqual(cfg.shopify_store, "example.myshopify.com")
        self.assertEqual(cfg.clickhouse_url, "https://clickhouse.example:8443")

    def test_defaults_are_the_documented_ones(self):
        cfg = load()
        self.assertEqual(cfg.customers_table, "shopify_customers")
        self.assertEqual(cfg.page_size, 100)
        self.assertEqual(cfg.max_pages, 20)
        self.assertEqual(cfg.overlap_seconds, 600)
        self.assertEqual(cfg.budget_seconds, 240)
        self.assertEqual(cfg.backfill_days, 30)
        self.assertIsNone(cfg.backfill_from)
        self.assertFalse(cfg.dry_run)

    def test_overrides_are_read(self):
        cfg = load(
            CLICKHOUSE_CUSTOMERS_TABLE="my_customers",
            PAGE_SIZE="250",
            MAX_PAGES_PER_RUN="3",
            RUN_BUDGET_SECONDS="30",
            BACKFILL_FROM="2026-01-01T00:00:00Z",
            DRY_RUN="true",
        )
        self.assertEqual(cfg.customers_table, "my_customers")
        self.assertEqual(cfg.page_size, 250)
        self.assertEqual(cfg.max_pages, 3)
        self.assertEqual(cfg.budget_seconds, 30)
        self.assertEqual(cfg.backfill_from.year, 2026)
        self.assertTrue(cfg.dry_run)

    def test_missing_required_values_fail_here(self):
        for missing in REQUIRED:
            environ = {key: value for key, value in REQUIRED.items() if key != missing}
            with mock.patch.dict(os.environ, environ, clear=True):
                with self.assertRaises(ConfigError) as caught:
                    Settings.load()
            self.assertIn(missing, str(caught.exception))

    def test_a_non_numeric_setting_fails_here(self):
        with self.assertRaises(ConfigError) as caught:
            load(PAGE_SIZE="lots")
        self.assertIn("PAGE_SIZE", str(caught.exception))

    def test_an_unusable_table_name_is_rejected(self):
        # Every one of these would change the meaning of the INSERT the name is
        # interpolated into.
        for bad in ("shopify customers", "customers;DROP TABLE x", "1customers", "a.b"):
            with self.subTest(table=bad):
                with self.assertRaises(ConfigError) as caught:
                    load(CLICKHOUSE_CUSTOMERS_TABLE=bad)
                self.assertIn("CLICKHOUSE_CUSTOMERS_TABLE", str(caught.exception))

    def test_a_blank_table_name_takes_the_default(self):
        # config.env treats a blank value as unset, so this is a different case
        # from a malformed one: it falls back rather than failing.
        self.assertEqual(load(CLICKHOUSE_CUSTOMERS_TABLE="").customers_table, "shopify_customers")

    def test_the_tenant_ids_are_normalized(self):
        # Canonical form, so an upper-case id and a lower-case one are the same
        # row key rather than two.
        cfg = load(CASTOR_WORKSPACE_ID="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        self.assertEqual(cfg.workspace_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_a_malformed_tenant_id_is_rejected(self):
        for bad in ("not-a-uuid", "123", "", "11111111-1111-1111-1111-11111111111z"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError) as caught:
                    load(CASTOR_DATASET_ID=bad)
                self.assertIn("CASTOR_DATASET_ID", str(caught.exception))

    def test_a_leading_underscore_is_a_valid_identifier(self):
        self.assertEqual(load(CLICKHOUSE_CUSTOMERS_TABLE="_customers").customers_table, "_customers")

    def test_settings_are_immutable(self):
        cfg = load()
        with self.assertRaises(FrozenInstanceError):
            cfg.page_size = 1


if __name__ == "__main__":
    unittest.main()
