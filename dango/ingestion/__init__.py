"""
Dango Ingestion Module

Handles data loading from various sources (CSV, APIs, databases).
"""

from .csv_loader import CSVLoader
from .dlt_runner import DltPipelineRunner, run_sync
from .sources import CATEGORIES, SOURCE_REGISTRY, get_source_metadata

__all__ = [
    "DltPipelineRunner",
    "run_sync",
    "CSVLoader",
    "SOURCE_REGISTRY",
    "CATEGORIES",
    "get_source_metadata",
]
