"""Loop V1 Package C: explicit personal context snapshot boundary.

The caller explicitly selects a minimal list of personal context materials;
this module validates them and freezes a deterministic, immutable snapshot.
The module itself never scans directories, reads files, environment
variables, keychains, databases, networks, real vaults, private notes, or
production data: every material is a handwritten synthetic value supplied by
the caller, and invalid input fails before any analyst, resolver, or
persistent write.

Three material kinds are supported, mirroring the existing cognitive
contract names:

- ``confirmed_user_fact``: non-empty ``text``, explicit ``inferred=false``,
  and a synthetic ``confirmation_ref`` — it records only that the user once
  explicitly confirmed this material, never external truth;
- ``obsidian_record``: non-empty ``text`` and a synthetic ``record_ref`` —
  the name follows the existing contract, but this package only ever uses
  synthetic records and never touches a real Obsidian vault;
- ``profile_inference``: non-empty ``text``, ``basis`` and ``uncertainty`` —
  it must never carry a ``confirmation_ref`` / ``record_ref`` or pose as a
  confirmed user fact.

The snapshot enforces a strict field allowlist, a deterministic material
order, duplicate-reference rejection, reference-escape rejection,
Unicode/whitespace/control-character boundaries, and input deep copies.  Its
only internal state is the canonical JSON string plus its SHA-256 — two
immutable scalars — and construction always re-validates shape, canonical
form, and hash, so direct constructor calls can never forge an invalid or
inconsistent instance.  It is recomputable: canonical JSON + SHA-256 over
the normalized materials, and it carries no paths, credentials, environment
values, or undeclared fields.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field

PERSONAL_CONTEXT_SNAPSHOT_VERSION = "fragment-personal-context-v1"

MATERIAL_TYPES = ("confirmed_user_fact", "obsidian_record", "profile_inference")

MAX_TEXT_LENGTH = 2000
MAX_REF_LENGTH = 256
MAX_MATERIAL_COUNT = 64

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "confirmed_user_fact": frozenset(
        {"material_type", "text", "inferred", "confirmation_ref"}
    ),
    "obsidian_record": frozenset({"material_type", "text", "record_ref"}),
    "profile_inference": frozenset({"material_type", "text", "basis", "uncertainty"}),
}


class PersonalContextError(ValueError):
    """A proposed personal context material failed the snapshot boundary."""


def _utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validated_text(label: str, value: object) -> str:
    """Non-empty single-line text with strict Unicode/whitespace boundaries."""
    if not isinstance(value, str):
        raise PersonalContextError(f"{label} 必须是字符串")
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or not _utf8_encodable(value)
    ):
        raise PersonalContextError(f"{label} 含有控制字符或无法 UTF-8 编码")
    if value != value.strip():
        raise PersonalContextError(f"{label} 不得携带首尾空白")
    if not value or len(value) > MAX_TEXT_LENGTH:
        raise PersonalContextError(f"{label} 必须为 1-{MAX_TEXT_LENGTH} 个字符")
    return value


def _validated_synthetic_ref(label: str, value: object) -> str:
    """A safe synthetic reference: ``synthetic/`` prefix, no escape of any kind."""
    if not isinstance(value, str):
        raise PersonalContextError(f"{label} 必须是字符串")
    if (
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or not _utf8_encodable(value)
        or value != value.strip()
        or not value
        or len(value) > MAX_REF_LENGTH
    ):
        raise PersonalContextError(f"{label} 不是干净的合成引用")
    if not value.startswith("synthetic/") or "://" in value or "\\" in value:
        raise PersonalContextError(
            f"{label} 必须是 synthetic/ 前缀的相对合成引用，禁止 URI 与反斜杠"
        )
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise PersonalContextError(f"{label} 含有空、. 或 .. 路径分量，属于引用逃逸")
    return value


def _normalize_material(index: int, raw: object) -> dict[str, object]:
    label = f"materials[{index}]"
    if not isinstance(raw, Mapping):
        raise PersonalContextError(f"{label} 必须是对象")
    material_type = raw.get("material_type")
    if material_type not in _ALLOWED_KEYS:
        raise PersonalContextError(f"{label}.material_type 非法: {material_type!r}")
    allowed = _ALLOWED_KEYS[material_type]
    extra = set(raw) - allowed
    if extra:
        raise PersonalContextError(f"{label} 含有未声明字段: {sorted(extra)}")
    if set(raw) != allowed:
        raise PersonalContextError(f"{label} 缺少必需字段: {sorted(allowed - set(raw))}")
    material: dict[str, object] = {
        "material_type": material_type,
        "text": _validated_text(f"{label}.text", raw.get("text")),
    }
    if material_type == "confirmed_user_fact":
        if raw.get("inferred") is not False:
            raise PersonalContextError(
                f"{label} 已确认用户事实必须显式标记 inferred=false"
            )
        material["inferred"] = False
        material["confirmation_ref"] = _validated_synthetic_ref(
            f"{label}.confirmation_ref", raw.get("confirmation_ref")
        )
    elif material_type == "obsidian_record":
        material["record_ref"] = _validated_synthetic_ref(
            f"{label}.record_ref", raw.get("record_ref")
        )
    else:
        material["basis"] = _validated_text(f"{label}.basis", raw.get("basis"))
        material["uncertainty"] = _validated_text(
            f"{label}.uncertainty", raw.get("uncertainty")
        )
    return material


def _sort_key(material: Mapping[str, object]) -> tuple[str, str, str]:
    ref = material.get("confirmation_ref", material.get("record_ref", ""))
    return (
        str(material.get("material_type")),
        str(ref) if isinstance(ref, str) else "",
        str(material.get("text")),
    )


def _canonical_json_for(materials: Sequence[Mapping[str, object]]) -> str:
    return json.dumps(
        {
            "snapshot_version": PERSONAL_CONTEXT_SNAPSHOT_VERSION,
            "materials": list(materials),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _refs_of(materials: Sequence[Mapping[str, object]]) -> list[str]:
    return [
        str(material.get("confirmation_ref", material.get("record_ref")))
        for material in materials
        if material.get("material_type") != "profile_inference"
    ]


def _validated_canonical_materials(canonical_json: object) -> list[dict[str, object]]:
    """Re-validate a canonical payload: any direct construction must pass too.

    The payload must be the exact canonical encoding (sort_keys, compact
    separators) of the frozen envelope whose materials are already in
    normalized form — strict allowlists, deterministic order, and globally
    unique references.  Anything less is a forgery and is rejected.
    """
    if not isinstance(canonical_json, str):
        raise PersonalContextError("快照内部表示必须是规范化 JSON 字符串")
    try:
        payload = json.loads(canonical_json)
    except ValueError:
        raise PersonalContextError("快照内部表示不是合法 JSON") from None
    if not isinstance(payload, dict) or set(payload) != {
        "snapshot_version",
        "materials",
    }:
        raise PersonalContextError("快照内部表示形状非法")
    if payload.get("snapshot_version") != PERSONAL_CONTEXT_SNAPSHOT_VERSION:
        raise PersonalContextError("快照版本非法")
    materials = payload.get("materials")
    if not isinstance(materials, list) or len(materials) > MAX_MATERIAL_COUNT:
        raise PersonalContextError("快照材料列表非法")
    normalized = [
        _normalize_material(index, material)
        for index, material in enumerate(materials)
    ]
    # The stored materials must already be in exact normalized form, in
    # deterministic order, with no duplicate reference.
    if normalized != materials:
        raise PersonalContextError("快照材料不是规范化形式")
    if normalized != sorted(normalized, key=_sort_key):
        raise PersonalContextError("快照材料顺序非法")
    refs = _refs_of(normalized)
    if len(refs) != len(set(refs)):
        raise PersonalContextError("重复引用被拒绝：同一引用只能被选择一次")
    if _canonical_json_for(normalized) != canonical_json:
        raise PersonalContextError("快照内部表示不是精确的规范编码")
    return normalized


@dataclass(frozen=True)
class PersonalContextSnapshot:
    """One frozen, recomputable personal context snapshot.

    The only internal state is the canonical JSON string plus its SHA-256 —
    both immutable scalars, so no caller can mutate the frozen materials in
    place.  ``__post_init__`` re-validates the canonical shape, the
    normalized materials, and the hash, so even a direct constructor call
    can never produce an invalid or hash/content-inconsistent instance.
    Both sensitive fields are excluded from the repr so logs never see
    private material text or hashes; only the safe material count shows.
    Every accessor rebuilds fresh objects from the canonical JSON.
    """

    _canonical_json: str = field(repr=False)
    _snapshot_sha256: str = field(repr=False)
    _material_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        materials = _validated_canonical_materials(self._canonical_json)
        digest = hashlib.sha256(self._canonical_json.encode("utf-8")).hexdigest()
        if not isinstance(self._snapshot_sha256, str) or digest != self._snapshot_sha256:
            raise PersonalContextError("快照 SHA-256 与规范化内容不一致")
        object.__setattr__(self, "_material_count", len(materials))

    @property
    def snapshot_version(self) -> str:
        return PERSONAL_CONTEXT_SNAPSHOT_VERSION

    @property
    def material_count(self) -> int:
        return self._material_count

    @property
    def snapshot_sha256(self) -> str:
        return self._snapshot_sha256

    def _materials(self) -> list[dict[str, object]]:
        payload = json.loads(self._canonical_json)
        materials = payload["materials"]
        assert isinstance(materials, list)
        return materials

    def summary(self) -> dict[str, object]:
        """Version + count + SHA only — never a second copy of private text."""
        return {
            "snapshot_version": self.snapshot_version,
            "material_count": self.material_count,
            "snapshot_sha256": self._snapshot_sha256,
        }

    def material_projection(self) -> list[dict[str, object]]:
        """A fresh deep copy of the minimal material projection."""
        return [deepcopy(material) for material in self._materials()]

    def resolve(self, material_type: str, ref: str) -> Mapping[str, object] | None:
        """Re-fetch one reference-bearing material from the frozen snapshot."""
        key = "confirmation_ref" if material_type == "confirmed_user_fact" else (
            "record_ref" if material_type == "obsidian_record" else None
        )
        if key is None:
            return None
        for material in self._materials():
            if material.get("material_type") == material_type and material.get(key) == ref:
                return deepcopy(material)
        return None


def build_personal_context_snapshot(
    materials: Sequence[Mapping[str, object]],
) -> PersonalContextSnapshot:
    """Validate explicitly selected materials and freeze the snapshot.

    The input is deep-copied before validation; the normalized materials are
    sorted deterministically, duplicate references are rejected, and the
    resulting SHA-256 is recomputable from the snapshot contents alone.
    """
    if isinstance(materials, (str, bytes)) or not isinstance(materials, Sequence):
        raise PersonalContextError("materials 必须是显式提供的材料列表")
    if len(materials) > MAX_MATERIAL_COUNT:
        raise PersonalContextError(f"materials 最多 {MAX_MATERIAL_COUNT} 条")
    normalized = [
        _normalize_material(index, deepcopy(dict(raw)) if isinstance(raw, Mapping) else raw)
        for index, raw in enumerate(materials)
    ]
    refs = _refs_of(normalized)
    if len(refs) != len(set(refs)):
        raise PersonalContextError("重复引用被拒绝：同一引用只能被选择一次")
    normalized.sort(key=_sort_key)
    canonical_json = _canonical_json_for(normalized)
    return PersonalContextSnapshot(
        _canonical_json=canonical_json,
        _snapshot_sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
