"""Approved Loop V1 read-only memory adapter (Phase 1, frozen slice).

The adapter accepts only an exact allowlist supplied by the caller plus a
root directory. It never scans directories, never follows symlinks, rejects
``..`` segments, absolute foreign paths, non-regular files, digest drift,
unknown schemas, and oversized inputs. Formal Loop V1 assets keep
``content_lifecycle=published`` and ``evidence_level=unverified``; a user
confirmation is never presented as factual verification. Candidates keep
their ``mem:candidate:*`` identity and are linked to formal assets only via
the non-evidential ``derived_from_candidate`` memory relation — a candidate
node is never renamed or promoted in place. Asset nodes also project the
parsed, allowlisted ``source_fragment`` as a read-only, non-evidential
provenance field; it never alters lifecycle or evidence level. Graph runs
store only asset references and digests, never private content.

``ApprovedFragmentGraphMemoryAdapter`` is the second read-only memory
input: caller-supplied ``fragment-cognitive-graph-v1`` projections, each
bound to one exact candidate id and one normalized SHA-256. At most four
projections are accepted, with hard caps on total nodes, edges and label
size. Every projection is re-validated with the Loop product contract
(``fragment_loop.cognitive_product.validate_graph_projection``); digest
drift, duplicate candidates, unknown types, oversize input and namespace
pollution all fail closed. Nodes are deterministically mapped into the
isolated ``mem:graph:*`` namespace as ``mem:graph:<candidate_id>:<digest>``
where ``<digest>`` is the full SHA-256 of the NFC-normalized UTF-8 source
node id: the untrusted source id (which may legitimately contain ``/``,
``:``, spaces, ``..`` or ``exec:`` — real ``local_note:*`` ids do) never
enters the reference namespace and is kept verbatim in the read-only
``source_node_ref`` provenance field. Two distinct source ids collapsing
onto one memory ref fail closed as ``duplicate_ref``. Mapped nodes carry
``content_lifecycle=candidate_projection`` and
``evidence_level=unverified``; candidate knowledge cards can never be
promoted here because the mapped node carries no status field at all.
Projection edges become ``memory_relation`` records with
``source=extracted`` — knowledge edges, never execution edges.

``CompositeMemoryAdapter`` merges the Loop V1 adapter and the graph
adapter into one exact, stably sorted view. It never scans files; unknown
refs and ref collisions between the two adapters fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from fragment_loop.cognitive_product import validate_graph_projection
from graph_runtime.spec import digest_of

MEMORY_NODE_SCHEMA = "memory_node"
MEMORY_RELATION_SCHEMA = "memory_relation"

ASSET_SCHEMAS = frozenset(("loop-v1-cognitive-result", "loop-v1-knowledge-card"))
CANDIDATE_SCHEMA = "loop-v1-candidate"
KNOWN_SCHEMAS = frozenset((*ASSET_SCHEMAS, CANDIDATE_SCHEMA))

# Memory-relation provenance is closed: extracted | inferred | human_asserted
# | ambiguous. None of them may ever masquerade as a factual evidence level,
# and a memory relation can never become an execution edge.
RELATION_SOURCES = frozenset(("extracted", "inferred", "human_asserted", "ambiguous"))

MAX_FILE_BYTES = 256 * 1024
MAX_ENTRIES = 64

# Fragment graph-projection input caps: at most four approved candidate
# projections, with hard totals across all of them.
MAX_PROJECTIONS = 4
MAX_GRAPH_NODES = 512
MAX_GRAPH_EDGES = 1024
MAX_GRAPH_LABEL_CHARS = 256
MAX_GRAPH_LABEL_TOTAL_CHARS = 8 * 1024
MAX_CANDIDATE_ID_CHARS = 85
MAX_GRAPH_NODE_ID_CHARS = 96

# Candidate ids stay strict ref-safe segments, and the graph node memory
# identity is ``mem:graph:<candidate_id>:<64-hex digest>`` — with an
# 85-char candidate cap the longest possible ref is exactly the shared
# 160-char memory-ref limit enforced by ``_validated_ref``.
assert len("mem:graph:") + MAX_CANDIDATE_ID_CHARS + 1 + 64 <= 160

MemoryNodeKind = Literal["asset", "card", "candidate"]


class MemoryAccessError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise MemoryAccessError(code)


@dataclass(frozen=True)
class AllowEntry:
    """One exact allowlisted memory file.

    ``ref`` is the stable memory identity (``mem:asset:*`` /
    ``mem:candidate:*``). ``candidate_ref`` links a formal asset back to the
    candidate it was derived from (non-evidential, human-asserted mapping).
    """

    ref: str
    relative_path: str
    sha256: str
    schema: str
    candidate_ref: str | None = None


@dataclass(frozen=True)
class MemoryNode:
    id: str
    kind: MemoryNodeKind
    title: str
    content_lifecycle: str
    evidence_level: str
    digest: str
    candidate_ref: str | None
    candidate_id: str | None
    # Read-only, non-evidential provenance: the parsed source fragment ref.
    source_fragment: str | None

    def view(self) -> dict[str, Any]:
        return {
            "schema": MEMORY_NODE_SCHEMA,
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "content_lifecycle": self.content_lifecycle,
            "evidence_level": self.evidence_level,
            "digest": self.digest,
            "candidate_ref": self.candidate_ref,
            "candidate_id": self.candidate_id,
            "source_fragment": self.source_fragment,
        }


@dataclass(frozen=True)
class MemoryRelation:
    from_ref: str
    to_ref: str
    type: str
    source: str

    def view(self) -> dict[str, Any]:
        return {
            "schema": MEMORY_RELATION_SCHEMA,
            "from": self.from_ref,
            "to": self.to_ref,
            "type": self.type,
            "source": self.source,
        }


@dataclass(frozen=True)
class GraphProjectionEntry:
    """One exact approved candidate graph projection.

    ``graph_projection_sha256`` is the normalized (canonical JSON) digest
    of ``graph_projection``; the adapter recomputes it and fails closed on
    any drift.
    """

    candidate_id: str
    graph_projection: dict[str, Any]
    graph_projection_sha256: str


@dataclass(frozen=True)
class GraphMemoryNode:
    """One projection node mapped into the isolated ``mem:graph:*`` space."""

    id: str
    kind: Literal["graph_node"]
    source_node_ref: str
    candidate_ref: str
    node_type: str
    label: str | None
    content_lifecycle: str
    evidence_level: str
    digest: str

    def view(self) -> dict[str, Any]:
        return {
            "schema": MEMORY_NODE_SCHEMA,
            "id": self.id,
            "kind": self.kind,
            "source_node_ref": self.source_node_ref,
            "candidate_ref": self.candidate_ref,
            "node_type": self.node_type,
            "label": self.label,
            "content_lifecycle": self.content_lifecycle,
            "evidence_level": self.evidence_level,
            "digest": self.digest,
        }


def _validated_ref(ref: object) -> str:
    if not isinstance(ref, str) or not ref.startswith("mem:") or len(ref) > 160:
        _fail("invalid_ref")
    body = ref[4:]
    if not body or any(ord(character) < 0x20 for character in body):
        _fail("invalid_ref")
    if "/" in body or "\\" in body or ".." in body:
        _fail("invalid_ref")
    if body.startswith("exec:"):
        _fail("namespace_mixing")
    return ref


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


_ASSET_RESULT_KEYS = frozenset((
    "type",
    "title",
    "content_lifecycle",
    "evidence_level",
    "user_confirmed",
    "promoted_to_asset",
    "candidate_id",
    "source_fragment",
    "confirmed_at",
))
_ASSET_CARD_KEYS = frozenset((*_ASSET_RESULT_KEYS, "asset_kind", "card_id"))


def _parse_asset_markdown(raw: bytes, schema: str) -> dict[str, object]:
    """Parse a formal-asset markdown file's frontmatter header; body unread.

    The header is delimited by two strict ``---`` lines (first line and the
    closing line); every non-empty line inside must be ``key: JSON-value``.
    Missing required keys, duplicate keys, unknown keys, and JSON errors all
    fail closed as ``invalid_payload``. Content after the closing delimiter
    is never read.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("invalid_payload")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        _fail("invalid_payload")
    closing = 0
    for index in range(1, len(lines)):
        if lines[index] == "---":
            closing = index
            break
    if not closing:
        _fail("invalid_payload")
    payload: dict[str, object] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not key:
            _fail("invalid_payload")
        if key in payload:
            _fail("invalid_payload")
        try:
            payload[key] = json.loads(value.strip())
        except json.JSONDecodeError:
            _fail("invalid_payload")
    if schema == "loop-v1-knowledge-card":
        allowed = _ASSET_CARD_KEYS
    else:
        allowed = _ASSET_RESULT_KEYS
    if set(payload) != allowed:
        _fail("invalid_payload")
    if schema == "loop-v1-knowledge-card":
        if payload["type"] != "Loop知识卡" or payload["asset_kind"] != "knowledge_card":
            _fail("invalid_payload")
        card_id = payload["card_id"]
        if not isinstance(card_id, str) or not card_id.strip():
            _fail("invalid_payload")
    else:
        if payload["type"] != "用户确认的认知整理结果":
            _fail("invalid_payload")
    if payload["content_lifecycle"] != "published":
        _fail("invalid_payload")
    if payload["evidence_level"] != "unverified":
        _fail("invalid_payload")
    if payload["user_confirmed"] is not True or payload["promoted_to_asset"] is not True:
        _fail("invalid_payload")
    for field in ("title", "candidate_id", "source_fragment"):
        field_value = payload[field]
        if not isinstance(field_value, str) or not field_value.strip():
            _fail("invalid_payload")
    return payload


def resolve_allowlisted_path(root: Path, relative_path: str) -> Path:
    """Resolve one exact allowlisted relative path under ``root``.

    Every segment is walked with ``lstat``: any symlink (dangling
    included) at any level is rejected before anything is resolved, as
    are ``..`` segments, absolute paths and non-regular files.
    """
    if not isinstance(relative_path, str) or not relative_path:
        _fail("invalid_path")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        _fail("absolute_path")
    parts = candidate.parts
    if any(part in ("..", "") for part in parts):
        _fail("parent_traversal")
    current = root
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            _fail("not_regular_file")
        if stat.S_ISLNK(mode):
            _fail("symlink_rejected")
    resolved = current.resolve()
    if resolved != root and root not in resolved.parents:
        _fail("path_escape")
    if not resolved.is_file():
        _fail("not_regular_file")
    return resolved


class ApprovedLoopV1MemoryAdapter:
    """Fail-closed read-only view over an exact allowlist; never scans."""

    def __init__(self, root: str | Path, entries: list[AllowEntry]):
        root_path = Path(root)
        if not root_path.is_dir():
            _fail("root_unavailable")
        self._root = root_path.resolve()
        if len(entries) > MAX_ENTRIES:
            _fail("too_many_entries")
        self._nodes: dict[str, MemoryNode] = {}
        self._relations: list[MemoryRelation] = []
        seen_refs: set[str] = set()
        for entry in entries:
            self._load_entry(entry, seen_refs)

    def _resolve(self, relative_path: str) -> Path:
        return resolve_allowlisted_path(self._root, relative_path)

    def _load_entry(self, entry: AllowEntry, seen_refs: set[str]) -> None:
        ref = _validated_ref(entry.ref)
        if ref in seen_refs:
            _fail("duplicate_ref")
        seen_refs.add(ref)
        if entry.schema not in KNOWN_SCHEMAS:
            _fail("unknown_schema")
        # Ref namespace is bound to the schema: formal assets live under
        # mem:asset:* and must name their origin candidate; the candidate
        # schema lives under mem:candidate:* and never has one.
        if entry.schema in ASSET_SCHEMAS:
            if not ref.startswith("mem:asset:") or entry.candidate_ref is None:
                _fail("ref_schema_mismatch")
        else:
            if not ref.startswith("mem:candidate:") or entry.candidate_ref is not None:
                _fail("ref_schema_mismatch")
        if not _is_sha256(entry.sha256):
            _fail("invalid_digest")
        candidate_ref = (
            _validated_ref(entry.candidate_ref) if entry.candidate_ref is not None else None
        )
        if candidate_ref is not None and not candidate_ref.startswith("mem:candidate:"):
            _fail("ref_schema_mismatch")
        path = self._resolve(entry.relative_path)
        size = path.stat().st_size
        if size < 1 or size > MAX_FILE_BYTES:
            _fail("input_too_large")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.sha256:
            _fail("digest_drift")
        candidate_id: str | None = None
        source_fragment: str | None = None
        title: object
        if entry.schema in ASSET_SCHEMAS:
            payload = _parse_asset_markdown(raw, entry.schema)
            candidate_id_value = payload["candidate_id"]
            assert isinstance(candidate_id_value, str)  # guaranteed by _parse_asset_markdown
            candidate_id = candidate_id_value
            if candidate_ref != f"mem:candidate:{candidate_id}":
                _fail("candidate_ref_mismatch")
            source_fragment_value = payload["source_fragment"]
            assert isinstance(source_fragment_value, str)  # guaranteed by _parse_asset_markdown
            source_fragment = source_fragment_value
            kind: MemoryNodeKind = "asset" if entry.schema == "loop-v1-cognitive-result" else "card"
            title = payload["title"]
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _fail("invalid_payload")
            if not isinstance(payload, dict) or payload.get("schema") != entry.schema:
                _fail("unknown_schema")
            if payload.get("status") != "candidate":
                _fail("lifecycle_drift")
            if payload.get("evidence_level", "unverified") != "unverified":
                _fail("evidence_level_drift")
            kind = "candidate"
            title = payload.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            _fail("invalid_title")
        self._nodes[ref] = MemoryNode(
            id=ref,
            kind=kind,
            title=title,
            content_lifecycle=("published" if entry.schema in ASSET_SCHEMAS else "candidate"),
            evidence_level="unverified",
            digest=entry.sha256,
            candidate_ref=candidate_ref,
            candidate_id=candidate_id,
            source_fragment=source_fragment,
        )
        if candidate_ref is not None:
            self._relations.append(
                MemoryRelation(
                    from_ref=ref,
                    to_ref=candidate_ref,
                    type="derived_from_candidate",
                    source="human_asserted",
                )
            )

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    def memory_view(self, refs: tuple[str, ...]) -> dict[str, Any]:
        """Read-only nodes for exactly the requested refs, plus the
        non-evidential provenance relations that originate from them.

        A relation is emitted whenever its ``from`` endpoint is selected,
        even when the target (for example the origin candidate) was not
        selected: the run sees the derivation reference while the
        unselected node — and its body — is never read.
        """
        unknown = [ref for ref in refs if ref not in self._nodes]
        if unknown:
            _fail("unknown_ref")
        selected = set(refs)
        relations = [
            relation for relation in self._relations if relation.from_ref in selected
        ]
        return {
            "nodes": [self._nodes[ref].view() for ref in sorted(selected)],
            "relations": [relation.view() for relation in relations],
        }


def _checked_candidate_id(value: object) -> str:
    """Candidate ids stay strict ref-safe segments: they are embedded in
    ``mem:candidate:*`` and ``mem:graph:*`` refs, so anything that could
    blur the ref structure or pollute the namespace fails closed."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CANDIDATE_ID_CHARS:
        _fail("invalid_candidate_id")
    if (
        any(character.isspace() for character in value)
        or "/" in value
        or "\\" in value
        or ".." in value
        or ":" in value
        or value.startswith("exec:")
    ):
        _fail("invalid_candidate_id")
    return value


def _checked_graph_node_id(value: object) -> str:
    """Validate a projection source node id as untrusted data.

    The id never enters the reference namespace — the memory identity is
    derived from its digest — so ``/``, ``:``, spaces, ``..`` and
    ``exec:`` are ordinary content (real ``local_note:*`` ids contain
    them). It must still be a non-empty string within the size cap and
    carry no control characters.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_GRAPH_NODE_ID_CHARS:
        _fail("invalid_node_id")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail("invalid_node_id")
    return value


def graph_node_digest(source_node_id: str) -> str:
    """Stable identity digest for one projection node.

    The full 64-hex SHA-256 is taken over the NFC-normalized UTF-8 form
    of the raw source node id (a uniform normalization policy: ids that
    differ only in Unicode normalization share one identity, and the
    adapter fails closed if two distinct raw ids collide on one ref).
    The digest — never the id itself — is embedded in the memory ref.
    """
    normalized = unicodedata.normalize("NFC", source_node_id)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ApprovedFragmentGraphMemoryAdapter:
    """Read-only memory view over approved candidate graph projections.

    Input is caller-supplied data only — exact candidate ids, parsed
    ``graph_projection`` objects and their normalized digests. The adapter
    never reads files, never scans directories and never invents nodes.
    """

    def __init__(self, projections: Sequence[GraphProjectionEntry]):
        if len(projections) > MAX_PROJECTIONS:
            _fail("too_many_projections")
        self._nodes: dict[str, GraphMemoryNode] = {}
        self._relations: list[MemoryRelation] = []
        seen_candidates: set[str] = set()
        totals = {"nodes": 0, "edges": 0, "label_chars": 0}
        for entry in projections:
            self._load_projection(entry, seen_candidates, totals)

    def _load_projection(
        self,
        entry: GraphProjectionEntry,
        seen_candidates: set[str],
        totals: dict[str, int],
    ) -> None:
        candidate_id = _checked_candidate_id(entry.candidate_id)
        candidate_ref = f"mem:candidate:{candidate_id}"
        _validated_ref(candidate_ref)
        if candidate_id in seen_candidates:
            _fail("duplicate_candidate")
        seen_candidates.add(candidate_id)
        if not _is_sha256(entry.graph_projection_sha256):
            _fail("invalid_digest")
        projection: object = entry.graph_projection
        if not isinstance(projection, dict):
            _fail("invalid_projection")
        # Summary drift: the supplied digest must match the normalized form
        # of the supplied projection exactly.
        if digest_of(projection) != entry.graph_projection_sha256:
            _fail("digest_drift")
        if validate_graph_projection(projection):
            _fail("invalid_projection")
        nodes_raw = list(projection["nodes"])
        edges_raw = list(projection["edges"])
        if totals["nodes"] + len(nodes_raw) > MAX_GRAPH_NODES:
            _fail("input_too_large")
        if totals["edges"] + len(edges_raw) > MAX_GRAPH_EDGES:
            _fail("input_too_large")
        totals["nodes"] += len(nodes_raw)
        totals["edges"] += len(edges_raw)
        id_map: dict[str, str] = {}
        seen_refs: dict[str, str] = {}
        for node in nodes_raw:
            node_id = _checked_graph_node_id(node.get("id"))
            ref = f"mem:graph:{candidate_id}:{graph_node_digest(node_id)}"
            # The candidate id is strict and the digest is lowercase hex,
            # so this cannot fail; the invariant is still verified, never
            # assumed.
            _validated_ref(ref)
            previous = seen_refs.get(ref)
            if previous is not None:
                # Two distinct source node ids collapsed onto one memory
                # ref (normalization twins or a digest collision): fail
                # closed rather than silently merging nodes.
                assert previous != node_id  # raw duplicates fail validation first
                _fail("duplicate_ref")
            seen_refs[ref] = node_id
            label: object = node.get("label")
            if label is not None:
                if not isinstance(label, str):
                    _fail("invalid_projection")
                if len(label) > MAX_GRAPH_LABEL_CHARS:
                    _fail("input_too_large")
                totals["label_chars"] += len(label)
                if totals["label_chars"] > MAX_GRAPH_LABEL_TOTAL_CHARS:
                    _fail("input_too_large")
            id_map[node_id] = ref
            self._nodes[ref] = GraphMemoryNode(
                id=ref,
                kind="graph_node",
                source_node_ref=node_id,
                candidate_ref=candidate_ref,
                node_type=str(node["type"]),
                label=label if isinstance(label, str) else None,
                # A projected node is never a formal asset; candidate cards
                # stay candidate/unverified and can never be promoted here.
                content_lifecycle="candidate_projection",
                evidence_level="unverified",
                digest=entry.graph_projection_sha256,
            )
        for edge in edges_raw:
            self._relations.append(
                MemoryRelation(
                    from_ref=id_map[str(edge["from"])],
                    to_ref=id_map[str(edge["to"])],
                    # The original knowledge-edge type is preserved, but the
                    # record is a memory_relation with source=extracted: it
                    # can never become an execution edge.
                    type=str(edge["type"]),
                    source="extracted",
                )
            )

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    def memory_view(self, refs: tuple[str, ...]) -> dict[str, Any]:
        """Read-only graph nodes for exactly the requested refs, plus the
        extracted knowledge relations that originate from them."""
        unknown = [ref for ref in refs if ref not in self._nodes]
        if unknown:
            _fail("unknown_ref")
        selected = set(refs)
        relations = [
            relation for relation in self._relations if relation.from_ref in selected
        ]
        return {
            "nodes": [self._nodes[ref].view() for ref in sorted(selected)],
            "relations": [relation.view() for relation in relations],
        }


MemoryAdapter = ApprovedLoopV1MemoryAdapter | ApprovedFragmentGraphMemoryAdapter


class CompositeMemoryAdapter:
    """Exact, stably sorted union of two approved memory adapters.

    The composite never scans files and never merges node bodies: it
    partitions the requested refs by owning adapter and concatenates the
    two read-only projections. A ref collision between the adapters fails
    closed at construction; an unknown ref fails closed at view time.
    """

    def __init__(self, first: MemoryAdapter, second: MemoryAdapter):
        if set(first.refs) & set(second.refs):
            _fail("duplicate_ref")
        self._first = first
        self._second = second

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(sorted((*self._first.refs, *self._second.refs)))

    def memory_view(self, refs: tuple[str, ...]) -> dict[str, Any]:
        known = set(self.refs)
        if any(ref not in known for ref in refs):
            _fail("unknown_ref")
        first_refs = set(self._first.refs)
        nodes: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for adapter, selected in (
            (self._first, tuple(ref for ref in refs if ref in first_refs)),
            (self._second, tuple(ref for ref in refs if ref not in first_refs)),
        ):
            if not selected:
                continue
            view = adapter.memory_view(selected)
            nodes.extend(view["nodes"])
            relations.extend(view["relations"])
        nodes.sort(key=lambda node: str(node["id"]))
        relations.sort(
            key=lambda relation: (
                str(relation["from"]),
                str(relation["to"]),
                str(relation["type"]),
            )
        )
        return {"nodes": nodes, "relations": relations}
