# -*- coding: utf-8 -*-
from .base import HotItem, parse_hot_value
from .sources import (
    ADAPTERS,
    SOURCE_NAMES,
    dedupe,
    fetch_all,
    fetch_custom,
    fetch_dailyhot,
    fetch_mock,
)

__all__ = [
    "HotItem",
    "parse_hot_value",
    "ADAPTERS",
    "SOURCE_NAMES",
    "fetch_all",
    "fetch_dailyhot",
    "fetch_mock",
    "fetch_custom",
    "dedupe",
]
