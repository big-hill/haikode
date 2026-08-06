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
    # Opaque provider-native reasoning blocks from this assistant turn, kept
    # so they can be handed back verbatim. Anthropic signs its thinking
    # blocks and requires them returned alongside the tool_use they preceded;
    # rebuilding the turn without them is what this field exists to prevent.
    # Shape: {"dialect": str, "model": str, "blocks": [ ... ]}. The tags are
    # not decoration: a signature is only valid to the dialect and model that
    # issued it, and replaying one anywhere else sends an opaque blob to a
    # provider that never made it.
    reasoning: Dict[str, Any] = field(default_factory=dict)


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
    # One finished provider-native reasoning block, verbatim, emitted when the
    # provider has seen all of it. `reasoning` above is the human-readable
    # stream for the screen; this is the machine's copy, signature included,
    # for handing back on the next request.
    reasoning_block: Optional[Dict[str, Any]] = None
    # Partial tool call: {"index": 0, "id": ..., "name": ..., "arguments": "<json fragment>"}
    tool_call_delta: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None  # stop | tool_calls | length | error
    usage: Optional[Dict[str, int]] = None


class ToolAborted(Exception):
    """Raised inside a tool when the user interrupts the run."""


class PermissionDenied(Exception):
    """Raised when the user rejects a permission request."""
