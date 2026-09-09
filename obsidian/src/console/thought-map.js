"use strict";

const THOUGHT_TYPES = new Set(["碎片想法", "已整理碎片", "碎片认知结果"]);
// 撤回只针对派生草稿/笔记；原始碎片想法即使误带 withdrawn 也不得从地图上消失。
const WITHDRAWABLE_TYPES = new Set(["已整理碎片", "碎片认知结果"]);
const STATUS_LABELS = {
  open: "待展开",
  provisional: "已有阶段判断",
  concluded: "已形成结论",
};

// Package E：新式认知笔记在 frontmatter 中携带与样式无关的主页投影字段。
// 控制台优先使用这些结构化字段，绝不从 Markdown 标题文本反向提取；
// 缺省或非法时完整回退到旧的标题/正文推导逻辑，旧笔记行为不变。
const HOME_SCHEMA_VERSION = "fragment-cognitive-home-v1";
const COMPONENT_SLOTS = [
  "summary",
  "key_facts",
  "critical_corrections",
  "personal_connections",
  "blind_spots",
  "candidate_cards",
  "evidence_drawer",
];
const HOME_ACTIONS = new Set(["open_detail", "confirm_draft", "withdraw_draft"]);
const HOME_CREDIBILITIES = new Set(["high", "medium", "low", "insufficient"]);
const CREDIBILITY_LABELS = { high: "高", medium: "中", low: "低", insufficient: "不足" };
// 机器形态材料（哈希、运行标识、provider_id/prompt_tokens/ledger_path 等
// 机器字段与值、带凭据的 URL 参数、内部绝对路径）不得进入主页投影；
// 正常讨论 provider/token/ledger/账本 的语义句子允许输出。规则与后端
// cognitive_product.py 的 _FORBIDDEN_SURFACE_PATTERNS 保持一致。
const MACHINE_MATERIAL_PATTERNS = [
  /[0-9a-fA-F]{64}/,
  /\b[0-9a-fA-F]{40}\b/,
  /\bsha256\s*[:=]/i,
  /\brun[_-]?id\b/i,
  /\bsession[_-]?identity\b/i,
  /\bquote[_-]?catalog\b/i,
  /\bprovider[_-](id|name|key)\b/i,
  /\b(prompt|completion|total)[_-]tokens\b/i,
  /\btoken[_-](count|usage|id|key|secret)\b/i,
  /\b(access|refresh|api|bearer)[_-](token|key|secret)\b/i,
  /\bledger[_-](path|id|file|entry)\b/i,
  /[?&](token|key|sig|signature|credential)=/i,
  /\/(Users|home|private)\//,
  /~\//,
  /\b[A-Za-z]:[\\/]/,
];

function carriesMachineMaterial(value) {
  return MACHINE_MATERIAL_PATTERNS.some((pattern) => pattern.test(value));
}

function stringList(value, { allowEmpty = true } = {}) {
  if (!Array.isArray(value)) return null;
  if (!allowEmpty && !value.length) return null;
  if (!value.every((item) => typeof item === "string" && item.trim())) return null;
  return value.map((item) => item.trim());
}

// Validate the frozen home projection carried in frontmatter. Returns the
// projection, or null when any field is missing, drifted, upgraded beyond
// draft + unverified, or carries machine material — the caller then falls
// back to the legacy heading/body derivation unchanged.
function homeFromFrontmatter(frontmatter) {
  if (text(frontmatter.home_schema_version) !== HOME_SCHEMA_VERSION) return null;
  const title = text(frontmatter.title);
  const coreJudgment = text(frontmatter.core_judgment);
  const userValue = text(frontmatter.user_value);
  const credibility = text(frontmatter.credibility);
  if (!title || !coreJudgment || !userValue) return null;
  if ([title, coreJudgment, userValue].some(carriesMachineMaterial)) return null;
  if (!HOME_CREDIBILITIES.has(credibility)) return null;
  // 新产物必须同时保持 content_lifecycle/lifecycle_status=draft、
  // evidence_level=unverified、user_confirmed=false、promoted_to_asset=false；
  // 任何升级迹象都拒绝按新投影展示，调用方完整回退到旧逻辑，笔记不消失。
  if (text(frontmatter.content_lifecycle) !== "draft") return null;
  if (text(frontmatter.lifecycle_status) !== "draft") return null;
  if (text(frontmatter.evidence_level) !== "unverified") return null;
  if (frontmatter.user_confirmed !== false) return null;
  if (frontmatter.promoted_to_asset !== false) return null;
  const primaryTopic = text(frontmatter.primary_topic);
  if (carriesMachineMaterial(primaryTopic)) return null;
  const candidateCardCount = frontmatter.candidate_card_count;
  const unresolvedQuestionCount = frontmatter.unresolved_question_count;
  for (const count of [candidateCardCount, unresolvedQuestionCount]) {
    if (typeof count !== "number" || !Number.isInteger(count) || count < 0) return null;
  }
  const topicIds = stringList(frontmatter.topic_ids);
  if (topicIds === null || topicIds.some(carriesMachineMaterial)) return null;
  const availableActions = stringList(frontmatter.available_actions, { allowEmpty: false });
  if (
    availableActions === null
    || !availableActions.includes("open_detail")
    || !availableActions.every((action) => HOME_ACTIONS.has(action))
  ) return null;
  const componentSlots = stringList(frontmatter.component_slots, { allowEmpty: false });
  if (
    componentSlots === null
    || componentSlots.length !== COMPONENT_SLOTS.length
    || !COMPONENT_SLOTS.every((slot, index) => componentSlots[index] === slot)
  ) return null;
  return {
    schemaVersion: HOME_SCHEMA_VERSION,
    title,
    coreJudgment,
    userValue,
    credibility,
    credibilityLabel: CREDIBILITY_LABELS[credibility],
    lifecycleStatus: "draft",
    evidenceLevel: "unverified",
    candidateCardCount,
    unresolvedQuestionCount,
    primaryTopic,
    topicIds,
    availableActions,
    componentSlots,
  };
}

function text(value) {
  return String(value == null ? "" : value).trim();
}

function list(value) {
  if (Array.isArray(value)) return value.map(text).filter(Boolean);
  return text(value).split(/[，,\n]/).map((item) => item.trim()).filter(Boolean);
}

function basename(value) {
  return text(value).replace(/^\[\[/, "").replace(/\]\]$/, "").replace(/\.md$/, "").split("/").pop();
}

function markdownBody(markdown) {
  return text(markdown)
    .replace(/^---\n[\s\S]*?\n---\s*/, "")
    .replace(/<!--\s*minimum-value-json[\s\S]*?-->/g, "")
    .trim();
}

function section(markdown, headingPattern) {
  const lines = markdownBody(markdown).split("\n");
  const start = lines.findIndex((line) => /^#{2,6}\s+/.test(line) && headingPattern.test(line));
  if (start < 0) return "";
  const level = (lines[start].match(/^#+/) || [""])[0].length;
  const selected = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    const match = lines[index].match(/^(#+)\s+/);
    if (match && match[1].length <= level) break;
    selected.push(lines[index]);
  }
  return selected.join("\n").trim();
}

function firstParagraph(value) {
  const beforeNestedHeading = text(value).split(/\n(?=#+\s+)/, 1)[0];
  return beforeNestedHeading
    .split(/\n\s*\n/)
    .map((part) => part.replace(/^>\s?/gm, "").replace(/\s+/g, " ").trim())
    .find((part) => part && !/^```/.test(part)) || "";
}

function bulletItems(value) {
  return text(value)
    .split("\n")
    .map((line) => line.match(/^\s*[-*+]\s+(.*)$/)?.[1] || "")
    .map((item) => item.replace(/[；。]\s*$/, "").trim())
    .filter(Boolean);
}

function sourceKey(frontmatter, fallback) {
  return basename(
    frontmatter.source_fragment ||
    frontmatter.source_file ||
    frontmatter.fragment_id ||
    fallback
  ).replace(/-line-\d+$/, "");
}

function thoughtFromNote(note) {
  const frontmatter = note.frontmatter || {};
  const type = text(frontmatter.type);
  if (!THOUGHT_TYPES.has(type)) return null;
  // R1-OD：lifecycle 经 text() 规范化后精确为 withdrawn 的派生结果/整理笔记，
  // 不进入地图候选；withdrawn_at 单独存在不推断，大小写或未知值不猜测。
  if (text(frontmatter.content_lifecycle) === "withdrawn" && WITHDRAWABLE_TYPES.has(type)) return null;

  const body = markdownBody(note.content);
  const unknownSection = section(body, /(仍然未知|不确定内容|待核实|尚未决定)/);
  const unknowns = [
    ...list(frontmatter.thought_unknowns || frontmatter.unresolved_questions || frontmatter.uncertainties),
    ...bulletItems(unknownSection),
  ].filter((item, index, values) => values.indexOf(item) === index);
  const conclusion = firstParagraph(section(body, /(结论先行|简短整理结果|综合判断|结论)/));
  const rawSummary = body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#") && !/^https?:\/\//.test(line) && line !== "nigo-loop")
    .join(" ")
    .slice(0, 180);
  const summary = text(frontmatter.thought_summary || frontmatter.summary || conclusion || rawSummary);
  const explicitStatus = text(frontmatter.thought_status);
  const status = Object.prototype.hasOwnProperty.call(STATUS_LABELS, explicitStatus)
    ? explicitStatus
    : type === "碎片想法"
      ? "open"
      : unknowns.length
        ? "provisional"
        : type === "碎片认知结果"
          ? "concluded"
          : "provisional";
  const heading = body.match(/^#\s+(.+)$/m)?.[1]?.replace(/^草稿[：:]\s*/, "");
  const category = text(frontmatter.thought_category || frontmatter.category) || "未归类";
  const rank = type === "碎片认知结果" ? 3 : type === "已整理碎片" ? 2 : 1;
  // Package E：新式认知笔记优先使用 frontmatter 中的结构化主页投影；
  // 投影缺失或非法时完全沿用旧的标题/正文推导，旧笔记行为不变。
  const home = homeFromFrontmatter(frontmatter);

  return {
    path: note.path,
    sourceKey: sourceKey(frontmatter, note.basename),
    title: home ? home.title : text(frontmatter.title || heading || note.basename) || "未命名思考",
    category,
    summary: home ? home.coreJudgment : summary || "这条思考尚未展开整理。",
    unknowns,
    status,
    statusLabel: STATUS_LABELS[status],
    updatedAt: text(frontmatter.thought_updated_at || frontmatter.updated_at || note.mtime),
    rank,
    home,
  };
}

function buildThoughtMap(entries) {
  const latestBySource = new Map();
  for (const candidate of entries.filter(Boolean)) {
    const unknowns = list(candidate.unknowns);
    const requestedStatus = STATUS_LABELS[candidate.status] ? candidate.status : "open";
    const status = requestedStatus === "concluded" && unknowns.length ? "provisional" : requestedStatus;
    const entry = {
      ...candidate,
      category: text(candidate.category) || "未归类",
      unknowns,
      status,
      statusLabel: STATUS_LABELS[status],
      rank: Number(candidate.rank || 0),
    };
    const key = text(entry.sourceKey || entry.path);
    const current = latestBySource.get(key);
    const newer = text(entry.updatedAt).localeCompare(text(current?.updatedAt)) > 0;
    if (!current || entry.rank > current.rank || (entry.rank === current.rank && newer)) latestBySource.set(key, entry);
  }

  const items = [...latestBySource.values()].sort((left, right) =>
    text(right.updatedAt).localeCompare(text(left.updatedAt)) || left.title.localeCompare(right.title, "zh-CN")
  );
  const grouped = new Map();
  for (const item of items) {
    if (!grouped.has(item.category)) grouped.set(item.category, []);
    grouped.get(item.category).push(item);
  }
  const categories = [...grouped.entries()]
    .sort(([left], [right]) => (left === "未归类" ? 1 : right === "未归类" ? -1 : left.localeCompare(right, "zh-CN")))
    .map(([name, categoryItems]) => ({ name, items: categoryItems }));
  const counts = { open: 0, provisional: 0, concluded: 0 };
  for (const item of items) counts[item.status] += 1;
  return { total: items.length, counts, categories };
}

function hashString(value) {
  let hash = 0;
  for (const char of text(value)) {
    hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  }
  return hash;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

const GOLDEN_ANGLE = 2.399963229728653; // radians

// 3D ring geometry for the homepage thought-map carousel. Pure math, no DOM:
// N theme cards sit evenly around a horizontal circle; the ring's current
// rotation decides each card's effective angle (0 = facing the reader), and
// every visual property the renderer needs (opacity, scale, blur, z-index)
// derives from the angular distance to the front. Keeping this pure makes the
// motion testable under plain Node.
const RING_CARD_WIDTH = 320;
const RING_CARD_GAP = 34;

function normalizeAngle(angle) {
  return (((Number(angle) || 0) % 360) + 540) % 360 - 180;
}

function computeRingLayout(count) {
  const total = Math.max(0, Math.floor(Number(count) || 0));
  if (!total) return { count: 0, step: 0, radius: 0 };
  const step = 360 / total;
  const radius = total === 1
    ? 0
    : Math.max(300, Math.round((RING_CARD_WIDTH + RING_CARD_GAP) / (2 * Math.tan(Math.PI / total))));
  return { count: total, step, radius };
}

function computeRingCardStates(count, rotation) {
  const layout = computeRingLayout(count);
  const spin = Number(rotation) || 0;
  return Array.from({ length: layout.count }, (_, index) => {
    const angle = normalizeAngle(index * layout.step - spin);
    const depth = (Math.cos((angle * Math.PI) / 180) + 1) / 2;
    return {
      index,
      angle,
      depth,
      front: Math.abs(angle) <= layout.step / 2,
      opacity: 0.15 + 0.85 * depth,
      scale: 0.78 + 0.22 * depth,
      zIndex: Math.round(depth * 100),
    };
  });
}

// Rotation value that brings card `index` to the front along the shortest arc
// from the current rotation, so click-to-focus never spins the long way round.
function ringRotationForIndex(index, count, rotation) {
  const layout = computeRingLayout(count);
  if (!layout.count) return 0;
  const current = Number(rotation) || 0;
  return current + normalizeAngle(index * layout.step - current);
}

// Deterministic star positions for the mini constellation drawn inside one
// theme card: each thought becomes a star scattered on a golden-angle spiral
// around the panel's centre with a per-item hash jitter, spread wide enough to
// fill the mini night-sky panel. Output is percentage-based, clamped to safe
// margins, and stable for the same input, which keeps rendering testable.
function computeConstellationPositions(items) {
  const entries = Array.isArray(items) ? items : [];
  const radiusMax = Math.min(44, 20 + entries.length * 3);
  return entries.map((item, index) => {
    const hash = hashString(`${item?.sourceKey || item?.path || item?.title || "star"}#${index}`);
    const angleJitter = (((hash % 60) - 30) * Math.PI) / 180;
    const angle = index * GOLDEN_ANGLE + angleJitter;
    const spread = (55 + ((hash >> 5) % 40)) / 100;
    const radius = entries.length === 1
      ? 0
      : Math.min(44, radiusMax * Math.sqrt((index + 0.6) / entries.length) * spread);
    return {
      item,
      x: clamp(50 + radius * Math.cos(angle), 8, 92),
      y: clamp(50 + radius * Math.sin(angle) * 0.9, 8, 92),
    };
  });
}

async function collectThoughtMap(app) {
  const files = app.vault.getMarkdownFiles().filter((file) => {
    const frontmatter = app.metadataCache.getFileCache(file)?.frontmatter || {};
    return THOUGHT_TYPES.has(text(frontmatter.type));
  });
  const notes = await Promise.all(files.map(async (file) => {
    const frontmatter = app.metadataCache.getFileCache(file)?.frontmatter || {};
    try {
      const content = await app.vault.cachedRead(file);
      return thoughtFromNote({
        path: file.path,
        basename: file.basename,
        mtime: file.stat?.mtime ? new Date(file.stat.mtime).toISOString() : "",
        frontmatter,
        content,
      });
    } catch {
      return null;
    }
  }));
  return buildThoughtMap(notes);
}

module.exports = {
  STATUS_LABELS,
  HOME_SCHEMA_VERSION,
  COMPONENT_SLOTS,
  thoughtFromNote,
  homeFromFrontmatter,
  buildThoughtMap,
  computeConstellationPositions,
  computeRingLayout,
  computeRingCardStates,
  ringRotationForIndex,
  normalizeAngle,
  collectThoughtMap,
};
