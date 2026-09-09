"""GraphSpec v1: frozen executable-agent-graph specification and validation.

Schema version is pinned to ``executable-agent-graph-spec-v1``. The SHA-256
of the canonical JSON form is the ``spec_digest`` referenced by runs, human
gates, and subgraph pins. Validation is fail-closed: unknown fields or
types, duplicate IDs, dangling edges, self loops, unreachable nodes,
non-terminal nodes without an exit, condition priority conflicts, unbounded
feedback, fan-in without a join, parallel partial failure without a policy,
action nodes without an explicit human gate, subgraph digest drift, and
execution/memory namespace mixing are all rejected.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, NoReturn

SCHEMA_VERSION = "executable-agent-graph-spec-v1"

EXECUTION_NAMESPACE = "exec:"
MEMORY_NAMESPACE = "mem:"

NodeKind = Literal[
    "input",
    "router",
    "capability",
    "validator",
    "human_decision",
    "action",
    "subgraph",
    "join",
    "output",
]
NODE_KINDS = frozenset(
    (
        "input",
        "router",
        "capability",
        "validator",
        "human_decision",
        "action",
        "subgraph",
        "join",
        "output",
    )
)

EdgeType = Literal["sequence", "condition", "feedback"]
EDGE_TYPES = frozenset(("sequence", "condition", "feedback"))

DecisionSource = Literal["declared", "rule_evaluated", "human_selected"]
DECISION_SOURCES = frozenset(("declared", "rule_evaluated", "human_selected"))

JOIN_MODES = frozenset(("all_success", "minimum_success"))
PARTIAL_FAILURE_POLICIES = frozenset(("fail", "continue", "pause"))
EXHAUSTED_POLICIES = frozenset(("fail", "pause", "route"))
SIDE_EFFECT_LEVELS = frozenset(("none", "local_write", "external"))
FIELD_TYPES = frozenset(("string", "integer", "number", "boolean", "object", "array", "any"))
CONDITION_OPS = frozenset(("equals", "not_equals", "in"))

# Human-gate binding is fixed by the runtime; the spec must pin exactly this
# set so an older or weaker binding cannot drift in silently.
HUMAN_GATE_BINDING_FIELDS = (
    "run_id",
    "node_id",
    "decision",
    "spec_digest",
    "input_digest",
    "expected_sequence",
    "requester",
    "decision_id",
)

SPEC_KEYS = frozenset(
    (
        "schema_version",
        "graph_id",
        "version",
        "goal",
        "entry_node",
        "nodes",
        "edges",
        "budgets",
        "policy_profile",
        "subgraphs",
    )
)
NODE_KEYS = frozenset(
    (
        "id",
        "kind",
        "input_schema",
        "output_schema",
        "adapter",
        "join",
        "human_gate",
        "action_policy",
        "subgraph_ref",
    )
)
EDGE_KEYS = frozenset(
    (
        "id",
        "from",
        "to",
        "type",
        "condition",
        "priority",
        "max_traversals",
        "on_exhausted",
        "exhausted_to",
        "decision_source",
    )
)
JOIN_KEYS = frozenset(("mode", "threshold", "on_partial_failure", "cancel_remaining"))
HUMAN_GATE_KEYS = frozenset(("allowed_decisions", "timeout_policy", "binding"))
ACTION_POLICY_KEYS = frozenset(("policy_profile", "required_gate", "side_effect_level"))
SUBGRAPH_REF_KEYS = frozenset(("graph_id", "version", "digest"))
CONDITION_KEYS = frozenset(("field", "op", "value"))
BUDGET_KEYS = frozenset(("max_node_executions",))
TERMINAL_KINDS = frozenset(("output", "action"))

MAX_ID_LENGTH = 128
MAX_GOAL_LENGTH = 512
MAX_NODES = 256
MAX_EDGES = 512


class GraphSpecError(ValueError):
    """Fail-closed spec rejection with a stable machine-readable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_json(value: Any) -> str:
    """Deterministic JSON form used for every Graph digest."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze spec content: dict → read-only mapping, list → tuple.

    Never reuses a passed-in MappingProxyType: it may wrap a backing dict
    the caller can still mutate, and aliasing it would let post-construction
    external mutation reach the spec's frozen content. Re-validation always
    rebuilds a fresh proxy over freshly frozen content, so repeated freezes
    stay content-stable without double-wrapping leaks.
    """
    if isinstance(value, Mapping):
        frozen = {key: _deep_freeze(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Recursively rebuild plain, freshly allocated JSON values.

    ``canonical()`` output goes through here, so a caller mutating the
    returned structure can never reach the spec's internal frozen content.
    """
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_deep_thaw(item) for item in value]
    return value


def plain_copy(value: Any) -> Any:
    """Public deep thaw: plain JSON copy of (possibly frozen) spec content.

    Runtime code that records spec fragments (e.g. an edge condition) into
    checkpoint state must go through here — frozen MappingProxyType/tuple
    internals are neither JSON-serializable nor deepcopyable, and state
    payloads must stay plain.
    """
    return _deep_thaw(value)


def _fail(code: str) -> NoReturn:
    raise GraphSpecError(code)


def _is_plain_identifier(value: object, *, what: str) -> str:
    """Graph IDs: NFC, case-sensitive, no control characters, no path
    semantics, and never in the execution or memory namespace."""
    if not isinstance(value, str):
        _fail(f"invalid_{what}")
    if not value or len(value) > MAX_ID_LENGTH:
        _fail(f"invalid_{what}")
    if unicodedata.normalize("NFC", value) != value:
        _fail(f"invalid_{what}")
    if value.startswith((EXECUTION_NAMESPACE, MEMORY_NAMESPACE)):
        _fail("namespace_mixing")
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or codepoint == 0x7F:
            _fail(f"invalid_{what}")
    if ":" in value or "/" in value or "\\" in value:
        _fail(f"invalid_{what}")
    if value in (".", "..") or value.startswith("."):
        _fail(f"invalid_{what}")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_field_schema(raw: object, *, what: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        _fail(f"invalid_{what}")
    schema: dict[str, str] = {}
    for key, value in raw.items():
        _is_plain_identifier(key, what=f"{what}_field")
        if value not in FIELD_TYPES:
            _fail(f"invalid_{what}")
        schema[str(key)] = str(value)
    return schema


def _validated_condition(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != CONDITION_KEYS:
        _fail("invalid_condition")
    _is_plain_identifier(raw["field"], what="condition_field")
    if raw["op"] not in CONDITION_OPS:
        _fail("invalid_condition")
    value = raw["value"]
    if raw["op"] == "in":
        if not isinstance(value, list) or not value:
            _fail("invalid_condition")
        for item in value:
            if not isinstance(item, (str, int, float, bool)) and item is not None:
                _fail("invalid_condition")
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        _fail("invalid_condition")
    return {"field": raw["field"], "op": raw["op"], "value": value}


def _validated_join(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != JOIN_KEYS:
        _fail("invalid_join")
    if raw["mode"] not in JOIN_MODES:
        _fail("invalid_join")
    threshold = raw["threshold"]
    if raw["mode"] == "minimum_success":
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            _fail("invalid_join")
    elif threshold is not None:
        _fail("invalid_join")
    if raw["on_partial_failure"] not in PARTIAL_FAILURE_POLICIES:
        _fail("invalid_join")
    if not isinstance(raw["cancel_remaining"], bool):
        _fail("invalid_join")
    if raw["mode"] == "all_success" and raw["cancel_remaining"]:
        # all_success must wait for every source to settle; cancelling the
        # remainder would contradict the mode, so the combination is refused.
        _fail("invalid_join")
    return {
        "mode": raw["mode"],
        "threshold": threshold,
        "on_partial_failure": raw["on_partial_failure"],
        "cancel_remaining": raw["cancel_remaining"],
    }


def _validated_human_gate(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != HUMAN_GATE_KEYS:
        _fail("invalid_human_gate")
    decisions = raw["allowed_decisions"]
    if (
        not isinstance(decisions, list)
        or not decisions
        or len(decisions) > 8
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in decisions)
        or len(set(decisions)) != len(decisions)
    ):
        _fail("invalid_human_gate")
    if raw["timeout_policy"] != "pause":
        _fail("invalid_human_gate")
    binding = raw["binding"]
    if not isinstance(binding, list) or set(binding) != set(HUMAN_GATE_BINDING_FIELDS):
        _fail("invalid_human_gate_binding")
    return {
        "allowed_decisions": [str(item) for item in decisions],
        "timeout_policy": "pause",
        "binding": list(HUMAN_GATE_BINDING_FIELDS),
    }


def _validated_action_policy(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != ACTION_POLICY_KEYS:
        _fail("invalid_action_policy")
    _is_plain_identifier(raw["policy_profile"], what="policy_profile")
    _is_plain_identifier(raw["required_gate"], what="required_gate")
    if raw["side_effect_level"] not in SIDE_EFFECT_LEVELS:
        _fail("invalid_action_policy")
    return {
        "policy_profile": str(raw["policy_profile"]),
        "required_gate": str(raw["required_gate"]),
        "side_effect_level": str(raw["side_effect_level"]),
    }


def _validated_subgraph_ref(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != SUBGRAPH_REF_KEYS:
        _fail("invalid_subgraph_ref")
    _is_plain_identifier(raw["graph_id"], what="subgraph_id")
    if not isinstance(raw["version"], str) or not raw["version"] or len(raw["version"]) > 64:
        _fail("invalid_subgraph_ref")
    if not _is_sha256(raw["digest"]):
        _fail("invalid_subgraph_ref")
    return {
        "graph_id": str(raw["graph_id"]),
        "version": str(raw["version"]),
        "digest": str(raw["digest"]),
    }


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: NodeKind
    input_schema: dict[str, str]
    output_schema: dict[str, str]
    adapter: str | None
    join: dict[str, Any] | None
    human_gate: dict[str, Any] | None
    action_policy: dict[str, Any] | None
    subgraph_ref: dict[str, Any] | None

    def __post_init__(self) -> None:
        # Gate 1 deep freeze: every nested mapping/list becomes read-only.
        object.__setattr__(self, "input_schema", _deep_freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _deep_freeze(self.output_schema))
        for field_name in ("join", "human_gate", "action_policy", "subgraph_ref"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _deep_freeze(value))

    def canonical(self) -> dict[str, Any]:
        # Fresh plain JSON values; mutating the result never reaches the
        # frozen internal content.
        return {
            "id": self.id,
            "kind": self.kind,
            "input_schema": _deep_thaw(self.input_schema),
            "output_schema": _deep_thaw(self.output_schema),
            "adapter": self.adapter,
            "join": _deep_thaw(self.join),
            "human_gate": _deep_thaw(self.human_gate),
            "action_policy": _deep_thaw(self.action_policy),
            "subgraph_ref": _deep_thaw(self.subgraph_ref),
        }


@dataclass(frozen=True)
class GraphEdge:
    id: str
    from_node: str
    to_node: str
    type: EdgeType
    condition: dict[str, Any] | None
    priority: int | None
    max_traversals: int | None
    on_exhausted: str | None
    exhausted_to: str | None
    decision_source: DecisionSource

    def __post_init__(self) -> None:
        if self.condition is not None:
            object.__setattr__(self, "condition", _deep_freeze(self.condition))

    def canonical(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.from_node,
            "to": self.to_node,
            "type": self.type,
            "condition": _deep_thaw(self.condition),
            "priority": self.priority,
            "max_traversals": self.max_traversals,
            "on_exhausted": self.on_exhausted,
            "exhausted_to": self.exhausted_to,
            "decision_source": self.decision_source,
        }


@dataclass(frozen=True)
class GraphSpec:
    graph_id: str
    version: str
    goal: str
    entry_node: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    budgets: dict[str, int]
    policy_profile: str
    subgraphs: tuple[dict[str, Any], ...]
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "budgets", _deep_freeze(self.budgets))
        object.__setattr__(
            self, "subgraphs", tuple(_deep_freeze(item) for item in self.subgraphs)
        )

    def canonical(self) -> dict[str, Any]:
        """The exact canonical v1 mapping the pinned digest was computed from.

        Returns freshly allocated plain JSON values in the validator's field
        set; mutating the result never reaches the frozen internal content,
        and the serialized bytes are byte-identical to the pre-freeze form.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "graph_id": self.graph_id,
            "version": self.version,
            "goal": self.goal,
            "entry_node": self.entry_node,
            "nodes": [node.canonical() for node in self.nodes],
            "edges": [edge.canonical() for edge in self.edges],
            "budgets": _deep_thaw(self.budgets),
            "policy_profile": self.policy_profile,
            "subgraphs": [_deep_thaw(item) for item in self.subgraphs],
        }

    def integrity_digest(self) -> str:
        """Digest recomputed from current canonical content.

        Any bypass of the deep freeze (e.g. ``object.__setattr__`` fault
        injection) makes this differ from the pinned ``digest``; the runtime
        compares the two before every handler reservation and stops with
        ``spec_digest_mismatch`` on drift.
        """
        return digest_of(self.canonical())

    @property
    def node_map(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    @property
    def edge_map(self) -> dict[str, GraphEdge]:
        return {edge.id: edge for edge in self.edges}

    def out_edges(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.from_node == node_id]

    def in_edges(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.to_node == node_id]

    def downstream_closure(self, node_id: str) -> list[str]:
        """Static transitive successor set (affected analysis).

        Traverses only ``sequence`` and ``condition`` edges; ``feedback``
        edges are skipped. A feedback edge is a bounded control loop, not a
        causal data dependency, so invalidation propagation must not walk
        backwards through it — otherwise a reopened validator would mark its
        own upstream (and, via the loop, the whole graph) as affected.
        """
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for edge in self.out_edges(current):
                if edge.type == "feedback":
                    continue
                if edge.to_node not in seen:
                    seen.add(edge.to_node)
                    stack.append(edge.to_node)
        return sorted(seen)


def _validated_node(raw: object, *, subgraphs: dict[tuple[str, str], str]) -> GraphNode:
    if not isinstance(raw, dict) or not set(raw) <= NODE_KEYS:
        _fail("unknown_node_field")
    node_id = _is_plain_identifier(raw.get("id"), what="node_id")
    kind = raw.get("kind")
    if kind not in NODE_KINDS:
        _fail("invalid_node_kind")
    input_schema = _validated_field_schema(raw.get("input_schema", {}), what="input_schema")
    output_schema = _validated_field_schema(raw.get("output_schema", {}), what="output_schema")
    adapter = raw.get("adapter")
    join = raw.get("join")
    human_gate = raw.get("human_gate")
    action_policy = raw.get("action_policy")
    subgraph_ref = raw.get("subgraph_ref")
    for unexpected in (join, human_gate, action_policy, subgraph_ref):
        if unexpected is not None and not isinstance(unexpected, dict):
            _fail("invalid_node")

    parsed_join: dict[str, Any] | None = None
    parsed_human_gate: dict[str, Any] | None = None
    parsed_action_policy: dict[str, Any] | None = None
    parsed_subgraph_ref: dict[str, Any] | None = None
    parsed_adapter: str | None = None

    if kind == "join":
        if join is None or any(
            value is not None for value in (adapter, human_gate, action_policy, subgraph_ref)
        ):
            _fail("invalid_node")
        # Runtime-built output is frozen: exactly {joined: array, skipped: array}.
        if output_schema != {"joined": "array", "skipped": "array"}:
            _fail("invalid_output_schema")
        parsed_join = _validated_join(join)
    elif kind == "human_decision":
        if human_gate is None or any(
            value is not None for value in (adapter, join, action_policy, subgraph_ref)
        ):
            _fail("invalid_node")
        # Runtime-built output is frozen: exactly {decision: string}.
        if output_schema != {"decision": "string"}:
            _fail("invalid_output_schema")
        parsed_human_gate = _validated_human_gate(human_gate)
    elif kind == "action":
        if action_policy is None or join is not None or human_gate is not None:
            _fail("invalid_node")
        if subgraph_ref is not None:
            _fail("invalid_node")
        if adapter is None:
            _fail("missing_adapter")
        parsed_adapter = _is_plain_identifier(adapter, what="adapter")
        parsed_action_policy = _validated_action_policy(action_policy)
    elif kind == "subgraph":
        if subgraph_ref is None or any(
            value is not None for value in (adapter, join, human_gate, action_policy)
        ):
            _fail("invalid_node")
        # Runtime-built output is frozen: exactly {subgraph: string, outputs: object}.
        if output_schema != {"subgraph": "string", "outputs": "object"}:
            _fail("invalid_output_schema")
        parsed_subgraph_ref = _validated_subgraph_ref(subgraph_ref)
        pin = subgraphs.get((parsed_subgraph_ref["graph_id"], parsed_subgraph_ref["version"]))
        if pin is None or pin != parsed_subgraph_ref["digest"]:
            _fail("subgraph_digest_drift")
    elif kind == "output":
        if any(
            value is not None for value in (adapter, join, human_gate, action_policy, subgraph_ref)
        ):
            _fail("invalid_node")
        # Runtime-built output is the merged upstream dict; the declared
        # output_schema must stay empty.
        if output_schema:
            _fail("invalid_output_schema")
    else:  # input, router, capability, validator
        if any(value is not None for value in (join, human_gate, action_policy, subgraph_ref)):
            _fail("invalid_node")
        if adapter is None:
            _fail("missing_adapter")
        parsed_adapter = _is_plain_identifier(adapter, what="adapter")

    return GraphNode(
        id=node_id,
        kind=kind,
        input_schema=input_schema,
        output_schema=output_schema,
        adapter=parsed_adapter,
        join=parsed_join,
        human_gate=parsed_human_gate,
        action_policy=parsed_action_policy,
        subgraph_ref=parsed_subgraph_ref,
    )


def _validated_edge(raw: object) -> GraphEdge:
    if not isinstance(raw, dict) or not set(raw) <= EDGE_KEYS:
        _fail("unknown_edge_field")
    edge_id = _is_plain_identifier(raw.get("id"), what="edge_id")
    from_node = _is_plain_identifier(raw.get("from"), what="edge_from")
    to_node = _is_plain_identifier(raw.get("to"), what="edge_to")
    edge_type = raw.get("type")
    if edge_type not in EDGE_TYPES:
        _fail("invalid_edge_type")
    condition = raw.get("condition")
    priority = raw.get("priority")
    max_traversals = raw.get("max_traversals")
    on_exhausted = raw.get("on_exhausted")
    exhausted_to = raw.get("exhausted_to")
    decision_source = raw.get("decision_source")
    if decision_source not in DECISION_SOURCES:
        _fail("invalid_decision_source")

    parsed_condition: dict[str, Any] | None = None
    parsed_priority: int | None = None
    parsed_max_traversals: int | None = None
    parsed_on_exhausted: str | None = None
    parsed_exhausted_to: str | None = None

    if edge_type == "sequence":
        if any(
            value is not None
            for value in (condition, priority, max_traversals, on_exhausted, exhausted_to)
        ):
            _fail("invalid_edge")
    elif edge_type == "condition":
        if condition is None or priority is None:
            _fail("invalid_edge")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            _fail("invalid_edge")
        if max_traversals is not None or on_exhausted is not None or exhausted_to is not None:
            _fail("invalid_edge")
        parsed_condition = _validated_condition(condition)
        parsed_priority = priority
    else:  # feedback
        if condition is None or max_traversals is None or on_exhausted is None:
            _fail("unbounded_feedback")
        if not isinstance(max_traversals, int) or isinstance(max_traversals, bool):
            _fail("unbounded_feedback")
        if max_traversals < 1 or max_traversals > 16:
            _fail("unbounded_feedback")
        if on_exhausted not in EXHAUSTED_POLICIES:
            _fail("invalid_edge")
        if on_exhausted == "route":
            if not isinstance(exhausted_to, str):
                _fail("invalid_edge")
            parsed_exhausted_to = _is_plain_identifier(exhausted_to, what="exhausted_to")
        elif exhausted_to is not None:
            _fail("invalid_edge")
        if priority is not None:
            _fail("invalid_edge")
        parsed_condition = _validated_condition(condition)
        parsed_max_traversals = max_traversals
        parsed_on_exhausted = str(on_exhausted)

    return GraphEdge(
        id=edge_id,
        from_node=from_node,
        to_node=to_node,
        type=edge_type,
        condition=parsed_condition,
        priority=parsed_priority,
        max_traversals=parsed_max_traversals,
        on_exhausted=parsed_on_exhausted,
        exhausted_to=parsed_exhausted_to,
        decision_source=decision_source,
    )


def validate_graph_spec(raw: object) -> GraphSpec:
    """Validate a raw spec mapping fail-closed and return the frozen spec."""
    if not isinstance(raw, dict) or set(raw) != SPEC_KEYS:
        _fail("unknown_spec_field")
    if raw["schema_version"] != SCHEMA_VERSION:
        _fail("invalid_schema_version")
    graph_id = _is_plain_identifier(raw["graph_id"], what="graph_id")
    version = raw["version"]
    if not isinstance(version, str) or not version or len(version) > 64:
        _fail("invalid_version")
    goal = raw["goal"]
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_LENGTH:
        _fail("invalid_goal")
    entry_node = _is_plain_identifier(raw["entry_node"], what="entry_node")
    policy_profile = _is_plain_identifier(raw["policy_profile"], what="policy_profile")

    budgets_raw = raw["budgets"]
    if not isinstance(budgets_raw, dict) or not budgets_raw or not set(budgets_raw) <= BUDGET_KEYS:
        _fail("invalid_budgets")
    budgets: dict[str, int] = {}
    for key, value in budgets_raw.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            _fail("invalid_budgets")
        budgets[str(key)] = value

    subgraphs_raw = raw["subgraphs"]
    if not isinstance(subgraphs_raw, list) or len(subgraphs_raw) > 32:
        _fail("invalid_subgraphs")
    subgraphs: dict[tuple[str, str], str] = {}
    for item in subgraphs_raw:
        ref = _validated_subgraph_ref(item)
        pin_key = (ref["graph_id"], ref["version"])
        if pin_key in subgraphs:
            _fail("duplicate_subgraph")
        subgraphs[pin_key] = ref["digest"]

    nodes_raw = raw["nodes"]
    if (
        not isinstance(nodes_raw, list)
        or not nodes_raw
        or len(nodes_raw) > MAX_NODES
        or any(not isinstance(item, dict) for item in nodes_raw)
    ):
        _fail("invalid_nodes")
    node_ids: list[str] = []
    for item in nodes_raw:
        node_ids.append(_is_plain_identifier(item.get("id"), what="node_id"))
    if len(set(node_ids)) != len(node_ids):
        _fail("duplicate_node_id")
    nodes = tuple(_validated_node(item, subgraphs=subgraphs) for item in nodes_raw)
    node_map = {node.id: node for node in nodes}
    if entry_node not in node_map:
        _fail("dangling_edge")

    edges_raw = raw["edges"]
    if not isinstance(edges_raw, list) or len(edges_raw) > MAX_EDGES:
        _fail("invalid_edges")
    edges = tuple(_validated_edge(item) for item in edges_raw)
    edge_ids = [edge.id for edge in edges]
    if len(set(edge_ids)) != len(edge_ids):
        _fail("duplicate_edge_id")
    for edge in edges:
        if edge.from_node not in node_map or edge.to_node not in node_map:
            _fail("dangling_edge")
        if edge.from_node == edge.to_node:
            _fail("self_loop")
        if edge.exhausted_to is not None and edge.exhausted_to not in node_map:
            _fail("dangling_edge")

    # Directed cycle detection over sequence/condition edges only; bounded
    # feedback edges are the sole permitted way to loop back.
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        if edge.type in ("sequence", "condition"):
            adjacency[edge.from_node].append(edge.to_node)
    visit_state: dict[str, int] = {}  # 1 = in stack, 2 = done
    for start in node_ids:
        if start in visit_state:
            continue
        stack = [(start, iter(adjacency[start]))]
        visit_state[start] = 1
        while stack:
            current, successors = stack[-1]
            advanced = False
            for successor in successors:
                state = visit_state.get(successor, 0)
                if state == 1:
                    _fail("non_feedback_cycle")
                if state == 0:
                    visit_state[successor] = 1
                    stack.append((successor, iter(adjacency[successor])))
                    advanced = True
                    break
            if not advanced:
                visit_state[current] = 2
                stack.pop()

    # Mutually exclusive condition priorities per source node.
    priorities: dict[str, set[int]] = {}
    for edge in edges:
        if edge.type != "condition":
            continue
        seen = priorities.setdefault(edge.from_node, set())
        assert edge.priority is not None
        if edge.priority in seen:
            _fail("condition_priority_conflict")
        seen.add(edge.priority)

    # Reachability from the entry node.
    reachable: set[str] = set()
    reachable_stack = [entry_node]
    while reachable_stack:
        current = reachable_stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for edge in edges:
            if edge.from_node == current:
                reachable_stack.append(edge.to_node)
    if reachable != set(node_ids):
        _fail("unreachable_node")

    # Non-terminal nodes must have an exit; fan-in requires a join node.
    for node in nodes:
        outgoing = [edge for edge in edges if edge.from_node == node.id]
        if not outgoing and node.kind not in TERMINAL_KINDS:
            _fail("node_without_exit")
        if node.kind == "join":
            continue
        inbound = [
            edge
            for edge in edges
            if edge.to_node == node.id and edge.type in ("sequence", "condition")
        ]
        if len(inbound) > 1:
            _fail("fan_in_without_join")

    # A minimum_success threshold can never exceed the number of direct
    # sequence/condition in-edges; otherwise the join could never fire.
    for node in nodes:
        if node.kind != "join":
            continue
        assert node.join is not None
        if node.join["mode"] != "minimum_success":
            continue
        inbound = [
            edge
            for edge in edges
            if edge.to_node == node.id and edge.type in ("sequence", "condition")
        ]
        if int(node.join["threshold"]) > len(inbound):
            _fail("invalid_join")

    # Action nodes require an explicit human gate node.
    for node in nodes:
        if node.kind != "action":
            continue
        assert node.action_policy is not None
        gate = node_map.get(node.action_policy["required_gate"])
        if gate is None or gate.kind != "human_decision":
            _fail("action_without_human_gate")
        if node.action_policy["policy_profile"] != policy_profile:
            _fail("policy_profile_mismatch")

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "version": version,
        "goal": goal,
        "entry_node": entry_node,
        "nodes": [node.canonical() for node in nodes],
        "edges": [edge.canonical() for edge in edges],
        "budgets": budgets,
        "policy_profile": policy_profile,
        "subgraphs": [
            {"graph_id": gid, "version": ver, "digest": subgraphs[(gid, ver)]}
            for gid, ver in sorted(subgraphs)
        ],
    }
    return GraphSpec(
        graph_id=graph_id,
        version=version,
        goal=goal,
        entry_node=entry_node,
        nodes=nodes,
        edges=edges,
        budgets=budgets,
        policy_profile=policy_profile,
        subgraphs=tuple(
            item for item in canonical["subgraphs"]  # type: ignore[misc]
        ),
        digest=digest_of(canonical),
    )
