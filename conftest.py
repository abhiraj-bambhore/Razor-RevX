"""
conftest.py — pytest configuration.
Suppresses broken system-level pytest plugins (web3/eth_typing incompatibility).
"""
import sys

# Patch out the broken web3 pytest plugin before it crashes
# (eth_typing incompatibility in this system's Python environment)
try:
    import web3.tools
except (ImportError, Exception):
    # Insert a mock so pytest11 entrypoint doesn't crash
    import types
    mock = types.ModuleType("web3.tools")
    mock.pytest_ethereum = types.ModuleType("web3.tools.pytest_ethereum")
    sys.modules["web3.tools"] = mock
    sys.modules["web3.tools.pytest_ethereum"] = mock.pytest_ethereum
