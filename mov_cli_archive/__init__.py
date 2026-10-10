from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mov_cli.plugins import PluginHookData

from .scraper import ArchiveScraper

plugin: PluginHookData = {
    "version": 1,  # plugin hook version
    "package_name": "mov-cli-archive",  # pypi package name
    "scrapers": {
        "DEFAULT": ArchiveScraper,
    },
}

__version__ = "1.7.0"