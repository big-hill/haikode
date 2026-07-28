"""
Canonical internal schema for messages, tools and streaming.
Provider-neutral — the OpenAI and Anthropic adapters both map onto this.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """A native function call requested by the model."""
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Msg:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""  # set on role="tool" results
    # Display-only: what the UI shows for a tool result (never sent upstream)
    display: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


@dataclass
class CompletionChunk:
    """One streaming delta. Exactly one field is meaningful per chunk."""
    text: str = ""
    reasoning: str = ""
    # Partial tool call: {"index": 0, "id": ..., "name": ..., "arguments": "<json fragment>"}
    tool_call_delta: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None  # stop | tool_calls | length | error
    usage: Optional[Dict[str, int]] = None


class ToolAborted(Exception):
    """Raised inside a tool when the user interrupts the run."""


class PermissionDenied(Exception):
    """Raised when the user rejects a permission request."""
