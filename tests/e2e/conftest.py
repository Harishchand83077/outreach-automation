"""The end-to-end suite reuses the integration suite's database fixtures.

Imported rather than copied, so there is one definition of what a test database is, one place
that applies the migrations, and no chance of the two suites drifting into testing against
different schemas — which would make a passing end-to-end run mean less than it appears to.

`_migrated_database` has to be imported explicitly: a star import skips underscore-prefixed
names, and every other fixture here depends on it.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  (re-exported as fixtures)
    _migrated_database,
    campaign,
    db,
    engine_factory,
    lead_factory,
    org,
    session_factory,
)
