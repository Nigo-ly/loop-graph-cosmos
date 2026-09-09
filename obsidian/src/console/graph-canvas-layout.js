"use strict";

// L3 deterministic layout (GRAPH-CANVAS-V1-DESIGN.md §2 rev2): ranks come ONLY
// from the sequence/condition DAG — feedback edges are excluded before rank
// computation and drawn afterwards as back-edges. Same-rank order is the
// node_id lexicographic order; parallel edges sort by edge_id. Identical
// input always yields identical coordinates.

const RANK_WIDTH = 260;
const ROW_HEIGHT = 150;
const NODE_WIDTH = 200;
const NODE_HEIGHT = 64;

function computeCanvasLayout(canvas) {
  const nodeIds = canvas.nodes.map((node) => node.node_id).sort();
  const dagEdges = canvas.edges.filter((edge) => edge.type !== "feedback");
  const feedbackEdges = canvas.edges.filter((edge) => edge.type === "feedback");

  const rank = new Map();
  const entry = canvas.nodes.find((node) => node.is_entry);
  const entryId = entry ? entry.node_id : nodeIds[0];
  for (const id of nodeIds) rank.set(id, 0);
  if (entryId) rank.set(entryId, 0);

  // Longest-path layering over the DAG. Iterations are capped at node count:
  // a valid spec has no sequence/condition cycle, and the cap keeps even a
  // malformed contract from looping forever.
  for (let pass = 0; pass < nodeIds.length; pass += 1) {
    let changed = false;
    const sorted = [...dagEdges].sort((a, b) => a.edge_id.localeCompare(b.edge_id));
    for (const edge of sorted) {
      if (!rank.has(edge.from) || !rank.has(edge.to)) continue;
      const candidate = rank.get(edge.from) + 1;
      if (candidate > rank.get(edge.to)) {
        rank.set(edge.to, candidate);
        changed = true;
      }
    }
    if (!changed) break;
  }

  const byRank = new Map();
  for (const id of nodeIds) {
    const r = rank.get(id) || 0;
    if (!byRank.has(r)) byRank.set(r, []);
    byRank.get(r).push(id);
  }

  let maxRank = 0;
  let maxRows = 1;
  for (const ids of byRank.values()) maxRows = Math.max(maxRows, ids.length);
  for (const r of byRank.keys()) maxRank = Math.max(maxRank, r);

  // 垂直居中：每列围绕画布中线对称排布，列内按 node_id 字典序。
  const centered = new Map();
  const canvasHeight = 120 + maxRows * ROW_HEIGHT;
  for (const [r, ids] of byRank.entries()) {
    ids.forEach((id, index) => {
      const y = canvasHeight / 2 + (index - (ids.length - 1) / 2) * ROW_HEIGHT - NODE_HEIGHT / 2;
      centered.set(id, { x: 60 + r * RANK_WIDTH, y });
    });
  }

  return {
    positions: centered,
    feedbackEdgeIds: feedbackEdges.map((edge) => edge.edge_id),
    width: 120 + (maxRank + 1) * RANK_WIDTH,
    height: canvasHeight,
    nodeWidth: NODE_WIDTH,
    nodeHeight: NODE_HEIGHT,
  };
}

module.exports = { computeCanvasLayout, RANK_WIDTH, ROW_HEIGHT, NODE_WIDTH, NODE_HEIGHT };
