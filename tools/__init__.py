# tools package
# Re-exports the current (v2) tool interface at the package level so callers
# can write `from tools import TOOLS, execute_tool` without specifying the version.
# To explicitly use v1 (legacy), import from tools.v1 directly.

from tools.v2 import TOOLS, execute_tool

__all__ = ["TOOLS", "execute_tool"]
