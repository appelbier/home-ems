"""Fixtures for the Home Assistant integration tests."""

from pathlib import Path
import sys

import custom_components

# The HA test plugin ships its own ``custom_components`` package; make this
# repository's integrations discoverable next to it.
REPO_COMPONENTS = str(Path(__file__).resolve().parents[2] / "custom_components")
if REPO_COMPONENTS not in custom_components.__path__:
    custom_components.__path__.append(REPO_COMPONENTS)

if sys.platform == "win32":
    import pytest_socket

    # The Windows event loop needs a local socket pair for its self-pipe,
    # which the HA test plugin's socket blocker refuses. HA itself does not
    # run on Windows; this only makes the tests runnable on a Windows PC.
    pytest_socket.disable_socket = lambda *args, **kwargs: None
