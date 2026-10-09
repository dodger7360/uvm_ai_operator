"""Compatibility entry point for the A3-only /price ingestion path."""

from pathlib import Path

from database import Database
from metal_reference.ingest_armatura_price import (
    DEFAULT_RESEARCH_DIR,
    ingest_armatura_price,
)


def ingest_armatura_a3(
    database: Database, research_dir: Path | str = DEFAULT_RESEARCH_DIR
) -> dict[str, int]:
    """Ingest the A3 category through the shared local /price importer."""
    return ingest_armatura_price(
        database, research_dir, category_codes=("armatura_a3",)
    )
