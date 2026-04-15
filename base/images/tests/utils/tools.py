# SPDX-License-Identifier: MIT
"""Native tool dependency registry.

Each utils module declares a ``REQUIRED_TOOLS`` list of :class:`NativeTool`
entries.  This module collects them into a single registry and provides
validation helpers used by ``pytest_configure``.

External callers can import :func:`required_tools` to get the full
list, or run this module directly::

    uv run python -m utils.tools          # human-readable
    uv run python -m utils.tools --json   # machine-readable
"""

from __future__ import annotations

import importlib
import pkgutil
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeTool:
    """A required external CLI tool."""

    name: str
    package_hint: str = ""
    reason: str = ""
    when: str = "always"  # "always", "vm", "container"


# ---------------------------------------------------------------------------
# Registry — auto-collected from REQUIRED_TOOLS in sibling modules.
# ---------------------------------------------------------------------------

def _collect() -> list[NativeTool]:
    """Import all modules in this package and collect their REQUIRED_TOOLS."""
    tools: list[NativeTool] = []
    package = importlib.import_module(__package__ or "utils")
    for info in pkgutil.iter_modules(package.__path__):
        mod = importlib.import_module(f"{__package__}.{info.name}")
        tools.extend(getattr(mod, "REQUIRED_TOOLS", []))
    return tools


def required_tools(when: str | None = None) -> list[NativeTool]:
    """Return required tools, optionally filtered by *when*.

    Args:
        when: ``"vm"``, ``"container"``, or ``None`` for all tools.
    """
    tools = _collect()
    if when is None:
        return tools
    return [t for t in tools if t.when in ("always", when)]


def check_tools(when: str | None = None) -> list[NativeTool]:
    """Return list of tools that are **missing** from ``$PATH``."""
    return [t for t in required_tools(when) if shutil.which(t.name) is None]


# ---------------------------------------------------------------------------
# CLI entry point: ``uv run python -m utils.tools [--json]``
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    fmt = "--json" in sys.argv

    tools = required_tools()
    missing = check_tools()

    if fmt:
        out = [
            {
                "name": t.name,
                "package_hint": t.package_hint,
                "reason": t.reason,
                "when": t.when,
                "available": shutil.which(t.name) is not None,
            }
            for t in tools
        ]
        print(json.dumps(out, indent=2))
    else:
        for t in tools:
            available = "✓" if shutil.which(t.name) else "✗ MISSING"
            scope = f"({t.when})" if t.when != "always" else ""
            hint = f"  [{t.package_hint}]" if t.package_hint else ""
            print(f"  {available:>9}  {t.name:<18} {scope:<14} {t.reason}{hint}")

        if missing:
            print(f"\n{len(missing)} missing tool(s). Install them before running tests.")
            sys.exit(1)
        else:
            print(f"\nAll {len(tools)} tools available.")
