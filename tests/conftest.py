"""Make the HA-free core modules importable without Home Assistant installed.

The integration package's ``__init__.py`` imports Home Assistant, so the core
modules are loaded through a bare package object pointing at the same folder.
"""

from pathlib import Path
import sys
import types

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "home_ems"

if "home_ems" not in sys.modules:
    package = types.ModuleType("home_ems")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["home_ems"] = package
