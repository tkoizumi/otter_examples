"""Every knob this integration reads, in one place.

Read as a group, before anything is built: a missing required value, a
non-numeric one or an unusable table name fails here rather than halfway
through a sync. ``main`` reads no environment variables itself.

Constructing a ``Settings`` directly is how the tests avoid touching
``os.environ``. That is the point of the dataclass rather than a handful of
module constants or another function-local block in ``main``.

Credentials are deliberately *not* here. ``SHOPIFY_CLIENT_ID``,
``CLICKHOUSE_USER`` and the rest are read by the client factories, because they
are per-deployment rather than per-sync. The target -- the store and the
ClickHouse endpoint -- is a setting, because naming it is a decision this
integration makes.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from otter_connectors.config import env, env_bool, env_int, require_env
from otter_connectors.errors import ConfigError
from otter_connectors.timeutil import parse_iso

__all__ = ["Settings"]

#: A bare ClickHouse identifier: letters, digits and underscores, not starting
#: with a digit. Table names are interpolated into an INSERT statement, so this
#: is a correctness boundary and not a style preference -- an unvalidated value
#: from the manifest would be SQL.
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table(name, value):
    """Validate a table name before it can reach a statement."""
    if not IDENTIFIER.match(value or ""):
        raise ConfigError(
            "%s must be a bare ClickHouse identifier (letters, digits, underscore), got %r"
            % (name, value)
        )
    return value


def _uuid(name, value):
    """Validate a Castor id, returning its canonical form.

    These lead the destination's sort key, so a malformed one is not a cosmetic
    problem: a value ClickHouse cannot parse fails the insert, and a *valid* but
    wrong id files every row under the wrong dataset without failing at all.
    Only the first is catchable here, which is why the manifest says where the
    three values come from.
    """
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise ConfigError("%s must be a UUID, got %r" % (name, value))


@dataclass(frozen=True)
class Settings:
    """What one run is configured to do.

    Grouped by what the values are *for* rather than by which module consumes
    them, so the destination settings read as one decision and the window
    settings as another.
    """

    # -- what we sync, and where it lands ---------------------------------- #
    shopify_store: str
    clickhouse_url: str
    customers_table: str

    # -- which dataset the rows belong to ---------------------------------- #
    #
    # Castor's three ids. One job instance syncs one dataset, so these are
    # configuration rather than per-record data, and they lead the destination's
    # sort key -- a wrong value files rows under the wrong dataset silently.
    workspace_id: str
    connection_id: str
    dataset_id: str

    # -- the window one run drains ----------------------------------------- #
    page_size: int
    max_pages: int
    overlap_seconds: int
    budget_seconds: int
    backfill_from: datetime | None
    backfill_days: int

    # -- mode --------------------------------------------------------------- #
    dry_run: bool

    @classmethod
    def load(cls):
        """Read every setting from the environment, failing fast on a bad one."""
        return cls(
            shopify_store=require_env("SHOPIFY_STORE"),
            clickhouse_url=require_env("CLICKHOUSE_URL"),
            customers_table=_table(
                "CLICKHOUSE_CUSTOMERS_TABLE", env("CLICKHOUSE_CUSTOMERS_TABLE", "shopify_customers")
            ),
            workspace_id=_uuid("CASTOR_WORKSPACE_ID", require_env("CASTOR_WORKSPACE_ID")),
            connection_id=_uuid("CASTOR_CONNECTION_ID", require_env("CASTOR_CONNECTION_ID")),
            dataset_id=_uuid("CASTOR_DATASET_ID", require_env("CASTOR_DATASET_ID")),
            page_size=env_int("PAGE_SIZE", 100),
            max_pages=env_int("MAX_PAGES_PER_RUN", 20),
            overlap_seconds=env_int("OVERLAP_SECONDS", 600),
            budget_seconds=env_int("RUN_BUDGET_SECONDS", 240),
            backfill_from=parse_iso(env("BACKFILL_FROM")),
            backfill_days=env_int("BACKFILL_DAYS", 30),
            dry_run=env_bool("DRY_RUN", False),
        )
