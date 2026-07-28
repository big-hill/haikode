"""
Tool registry. Mirrors opencode's tool set, minus the pieces that depend on
services Haiku doesn't have (ripgrep, tree-sitter, LSP is added separately).

The memory tools live in haikode/memory.py, which imports this package for the
Tool base class. That is a genuine import cycle, and which half loses depends
on who is imported first: `import haikode.memory` reaches this module in the
middle of memory's own import, when MEMORY_TOOLS does not exist yet. So the
registry is completed on first *access* rather than at import time —
`_register_memory()` is retried behind the module-level `__getattr__` below,
and the objects it hands out are the same ones it always was, mutated in place.
"""

from typing import Dict, List, Optional

from ..schema import ToolSpec
from .base import Tool, ToolContext, ToolResult
from .apply_patch import ApplyPatchTool
from .files import EditTool, ReadTool, WriteTool
from .misc import TodoWriteTool, WebFetchTool
from .question import QuestionTool
from .search import GlobTool, GrepTool, ListTool
from .shell import BashTool
from .skill import SkillTool
from .task import TaskTool

_ALL_TOOLS: List[Tool] = [
    ApplyPatchTool(),
    BashTool(),
    EditTool(),
    GlobTool(),
    GrepTool(),
    ListTool(),
    QuestionTool(),
    ReadTool(),
    SkillTool(),
    TaskTool(),
    TodoWriteTool(),
    WebFetchTool(),
    WriteTool(),
]

_REGISTRY: Dict[str, Tool] = {tool.name: tool for tool in _ALL_TOOLS}

_memory_registered = False


def _register_memory() -> None:
    """Add memory_write/memory_read once haikode.memory is importable.

    Silent on ImportError instead of raising: the only way it fails is the
    circular import described at the top of the module, and that resolves by
    itself as soon as memory finishes loading. A retry costs one bool test.
    """
    global _memory_registered
    if _memory_registered:
        return
    try:
        from ..memory import MEMORY_TOOLS
    except ImportError:
        return
    _memory_registered = True
    deferred = list(MEMORY_TOOLS)
    try:
        # Same circular-import dance: session imports tool.base, so it can
        # only contribute its tool once it has finished loading.
        from ..session import SESSION_TOOLS
        deferred.extend(SESSION_TOOLS)
    except ImportError:
        pass
    for tool in deferred:
        if tool.name in _REGISTRY:
            continue
        _ALL_TOOLS.append(tool)
        _REGISTRY[tool.name] = tool


def __getattr__(name: str):
    """Serve ALL_TOOLS/REGISTRY, completing the registry on the way out.

    PEP 562 module __getattr__ only fires for names absent from the module's
    globals, which is exactly why neither is assigned there: every reader goes
    through here and therefore sees the memory tools, no matter which module
    was imported first.
    """
    if name == "REGISTRY":
        _register_memory()
        return _REGISTRY
    if name == "ALL_TOOLS":
        _register_memory()
        return _ALL_TOOLS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_tools(names: Optional[List[str]] = None) -> Dict[str, Tool]:
    _register_memory()
    if names is None:
        return dict(_REGISTRY)
    return {name: _REGISTRY[name] for name in names if name in _REGISTRY}


def tool_specs(tools: Dict[str, Tool]) -> List[ToolSpec]:
    return [ToolSpec(name=t.name, description=t.description, parameters=t.parameters)
            for t in tools.values()]


# The common import order (anything that reaches the agent) gets a complete
# registry immediately; the lazy path above only matters for memory-first.
_register_memory()


__all__ = ["ALL_TOOLS", "REGISTRY", "get_tools", "tool_specs",
           "Tool", "ToolContext", "ToolResult"]
