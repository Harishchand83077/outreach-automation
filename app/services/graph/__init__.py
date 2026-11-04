"""The knowledge graph: what this workspace knows, and where each line came from (F12, ADR-051).

**Nothing is stored here without provenance**, and that is a database constraint rather than a
rule in this package: `ck_edges_provenance` requires a `source_url` or a `message_id`. A service
can be bypassed by the next writer; a check constraint cannot.
"""

from app.services.graph import store

__all__ = ["store"]
