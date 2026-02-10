"""
Dango Source Registry

Metadata registry for all supported data sources.
"""

from .registry import CATEGORIES, SOURCE_REGISTRY, get_source_metadata

__all__ = ["SOURCE_REGISTRY", "CATEGORIES", "get_source_metadata"]
