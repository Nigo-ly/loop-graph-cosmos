"use strict";

// L4 renderer / view state (GRAPH-CANVAS-V1-DESIGN.md §2 rev2): owns
// coordinates, colors, zoom, pan and session-only node drag offsets. All
// emphasis is static — no rAF loops, no CSS keyframes/transitions, no
// polling. Every dynamic string renders as a text node. Document-level drag
// listeners are removed immediately on pointerup/pointercancel, and dispose()
// clears anything left (including the click-suppression timer).

const SVG_NS = "http://www.w3.org/2000/svg";

// 每个画布实例独立的 SVG marker ID 前缀：同时打开两个 Graph 视图也不会冲突。
let canvasInstanceCounter = 0;

// 九类节点的图形标记：kind 类名驱动 CSS 形状（clip-path/圆角），marker 字符
// 提供不依赖颜色的第二编码。
const KIND_MARKERS = {
  input: "◧",
  router: "◆",
  capability: "▣",
  validator: "⬡",
  human_decision: "●",
  action: "➤",
  subgraph: "▤",
  join: "▼",
  output: "○",
};

function markerKindClass(kind) {
  return Object.prototype.hasOwnProperty.call(KIND_MARKERS, kind) ? kind : "unknown";
}

function createSvgElement(doc, tag) {
  if (doc && typeof doc.createElementNS === "function") return doc.createElementNS(SVG_NS, tag);
  return doc.createElement(tag);
}

function setStyles(element, declarations) {
  element.setAttribute("style", declarations);
}

// Obsidian/FakeEl 的元素都有 addClass；原生 SVG 退回 class 属性。
function addClasses(element, cls) {
  const names = String(cls).split(/\s+/).filter(Boolean);
  if (typeof element.addClass === "function") {
    for (const name of names) element.addClass(name);
  } else {
    element.setAttribute("class", `${element.getAttribute("class") || ""} ${names.join(" ")}`.trim());
  }
}

function edgePath(from, to, nodeWidth, nodeHeight, isFeedback) {
  const x1 = from.x + nodeWidth;
  const y1 = from.y + nodeHeight / 2;
  const x2 = to.x;
  const y2 = to.y + nodeHeight / 2;
  if (isFeedback) {
    const lift = 80;
    return `M ${x1 - nodeWidth / 2} ${from.y} C ${x1 - nodeWidth / 2} ${from.y - lift}, ${x2 + nodeWidth / 2} ${to.y - lift}, ${x2 + nodeWidth / 2} ${to.y}`;
  }
  const midX = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`;
}

function edgeLabelPoint(from, to, nodeWidth, nodeHeight, isFeedback) {
  const x1 = from.x + nodeWidth;
  const x2 = to.x;
  if (isFeedback) {
    return { x: (x1 + x2) / 2, y: Math.min(from.y, to.y) - 48 };
  }
  return { x: (x1 + x2) / 2, y: (from.y + to.y) / 2 + nodeHeight / 2 - 6 };
}

// 渲染只读工作流画布。返回 { dispose }；调用方在卸载/重渲/返回列表前必须调用。
function renderGraphCanvas(container, model, layout, handlers) {
  container.empty();
  const instanceId = ++canvasInstanceCounter;
  const sessionOffsets = new Map(); // 节点视觉位移：仅会话内存，刷新即弃
  const doc = container.ownerDocument || (typeof document !== "undefined" ? document : null);
  const docListeners = [];
  let suppressClick = false;
  let suppressTimer = null;

  const unlistenDoc = () => {
    if (doc && typeof doc.removeEventListener === "function") {
      for (const [name, fn] of docListeners) doc.removeEventListener(name, fn);
    }
    docListeners.length = 0;
  };
  const listenDoc = (name, fn) => {
    if (doc && typeof doc.addEventListener === "function") {
      doc.addEventListener(name, fn);
      docListeners.push([name, fn]);
    }
  };
  const dispose = () => {
    unlistenDoc();
    if (suppressTimer !== null && typeof window !== "undefined" && typeof window.clearTimeout === "function") {
      window.clearTimeout(suppressTimer);
    }
    suppressTimer = null;
  };

  const state = { scale: 1, panX: 0, panY: 0 };

  const header = container.createDiv({ cls: "graph-canvas-header" });
  header.createEl("span", { cls: "graph-canvas-task", text: model.taskLabel });
  header.createEl("span", { cls: `graph-canvas-status ${model.statusClass}`, text: model.statusLabel });
  const current = model.nodeMap.get(model.currentNode);
  // 技术 ID 不进主视觉：找不到当前节点时显示「未知节点」，不显示原始 node_id。
  header.createEl("span", {
    cls: "graph-canvas-current",
    text: `当前节点：${current ? current.label : "未知节点"} · Checkpoint #${model.sequence}`,
  });

  const stage = container.createDiv({ cls: "graph-canvas-stage" });
  const viewport = stage.createDiv({ cls: "graph-canvas-viewport" });
  const world = viewport.createDiv({ cls: "graph-canvas-world" });

  const applyView = () => {
    setStyles(
      world,
      `transform:translate(${state.panX}px, ${state.panY}px) scale(${state.scale});` +
        `width:${layout.width}px;height:${layout.height}px`
    );
  };

  const viewportRect = () =>
    typeof viewport.getBoundingClientRect === "function"
      ? viewport.getBoundingClientRect()
      : { left: 0, top: 0, width: 0, height: 0 };

  const fitView = () => {
    // 以画布实际 viewport（已扣除右侧参与栏）为准；首次进入即适配，
    // 不再要求用户先发现并点击“适配视图”。
    const rect = viewportRect();
    const vw = rect.width || viewport.clientWidth || 0;
    const vh = rect.height || viewport.clientHeight || 0;
    if (vw > 0 && vh > 0) {
      state.scale = Math.min(1, vw / layout.width, vh / layout.height);
      state.panX = (vw - layout.width * state.scale) / 2;
      state.panY = (vh - layout.height * state.scale) / 2;
    } else {
      state.scale = 1;
      state.panX = 0;
      state.panY = 0;
    }
    applyView();
  };

  const svg = createSvgElement(doc, "svg");
  addClasses(svg, "graph-canvas-edges");
  svg.setAttribute("width", String(layout.width));
  svg.setAttribute("height", String(layout.height));
  world.appendChild(svg);

  // 方向箭头：已走实色、未走弱化，两种 marker。
  const defs = createSvgElement(doc, "defs");
  for (const [id, cls] of [[`gc-arrow-taken-${instanceId}`, "is-taken"], [`gc-arrow-untaken-${instanceId}`, "is-untaken"]]) {
    const marker = createSvgElement(doc, "marker");
    marker.setAttribute("id", id);
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const arrow = createSvgElement(doc, "path");
    arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    addClasses(arrow, `graph-canvas-arrow ${cls}`);
    marker.appendChild(arrow);
    defs.appendChild(marker);
  }
  svg.appendChild(defs);

  const positionOf = (nodeId) => {
    const base = layout.positions.get(nodeId) || { x: 0, y: 0 };
    const offset = sessionOffsets.get(nodeId);
    return offset ? { x: base.x + offset.x, y: base.y + offset.y } : base;
  };

  const drawEdges = () => {
    svg.empty();
    svg.appendChild(defs);
    const feedbackSet = new Set(layout.feedbackEdgeIds);
    for (const edge of model.edges) {
      const from = positionOf(edge.from);
      const to = positionOf(edge.to);
      const isFeedback = feedbackSet.has(edge.edgeId);
      const path = createSvgElement(doc, "path");
      path.setAttribute("d", edgePath(from, to, layout.nodeWidth, layout.nodeHeight, isFeedback));
      addClasses(path, `graph-canvas-edge is-${edge.taken ? "taken" : "untaken"}${isFeedback ? " is-feedback" : ""}`);
      path.setAttribute("marker-end", `url(#gc-arrow-${edge.taken ? "taken" : "untaken"}-${instanceId})`);
      // 边参与交互：可点击、可 Tab 聚焦、Enter/空格打开选择说明（侧栏）。
      path.setAttribute("tabindex", "0");
      path.setAttribute("role", "button");
      path.setAttribute("aria-label", edge.summary);
      path.addEventListener("click", () => handlers.onSelectEdge(edge.edgeId));
      path.addEventListener("keydown", (event) => {
        if (event && (event.key === "Enter" || event.key === " ")) handlers.onSelectEdge(edge.edgeId);
      });
      svg.appendChild(path);
      // 可见边标签：条件映射/固定 label、返修 n/N；仅安全文案，无 reason 原文。
      const labelText = isFeedback ? edge.feedbackBadge : (edge.type === "condition" ? edge.label : null);
      if (labelText) {
        const point = edgeLabelPoint(from, to, layout.nodeWidth, layout.nodeHeight, isFeedback);
        const text = createSvgElement(doc, "text");
        text.setAttribute("x", String(point.x));
        text.setAttribute("y", String(point.y));
        addClasses(text, `graph-canvas-edge-label${isFeedback ? " is-feedback" : ""}`);
        text.setAttribute("text-anchor", "middle");
        text.textContent = labelText;
        svg.appendChild(text);
      }
    }
  };

  const nodeLayer = world.createDiv({ cls: "graph-canvas-nodes" });
  const nodeElements = new Map();

  let dragState = null; // { nodeId | "pan", startX, startY, baseX, baseY, moved }

  const endDrag = () => {
    if (dragState && dragState.moved) {
      suppressClick = true;
      if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
        suppressTimer = window.setTimeout(() => { suppressClick = false; suppressTimer = null; }, 0);
      } else {
        suppressClick = false;
      }
    }
    dragState = null;
    unlistenDoc(); // 每次 pointerup/cancel 后立即解除 document 监听
  };

  const onDocMove = (event) => {
    if (!dragState) return;
    const x = Number(event && event.clientX) || 0;
    const y = Number(event && event.clientY) || 0;
    const dx = x - dragState.startX;
    const dy = y - dragState.startY;
    if (Math.abs(dx) + Math.abs(dy) > 6) dragState.moved = true;
    if (dragState.nodeId === "pan") {
      state.panX = dragState.baseX + dx;
      state.panY = dragState.baseY + dy;
      applyView();
    } else {
      sessionOffsets.set(dragState.nodeId, {
        x: dragState.baseX + dx / state.scale,
        y: dragState.baseY + dy / state.scale,
      });
      placeNodes();
      drawEdges();
    }
  };

  const beginDocDrag = (drag) => {
    dragState = drag;
    listenDoc("pointermove", onDocMove);
    listenDoc("pointerup", endDrag);
    listenDoc("pointercancel", endDrag);
  };

  viewport.addEventListener("pointerdown", (event) => {
    beginDocDrag({ nodeId: "pan", startX: event.clientX, startY: event.clientY, baseX: state.panX, baseY: state.panY, moved: false });
  });
  viewport.addEventListener("wheel", (event) => {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    const delta = event && event.deltaY ? event.deltaY : 0;
    const next = Math.min(2.5, Math.max(0.4, state.scale * (delta > 0 ? 0.92 : 1.08)));
    // 以指针位置为锚点：缩放前后指针下的世界坐标保持不动。
    const rect = viewportRect();
    const px = (event && typeof event.clientX === "number" ? event.clientX : rect.left) - rect.left;
    const py = (event && typeof event.clientY === "number" ? event.clientY : rect.top) - rect.top;
    const ratio = next / state.scale;
    state.panX = px - (px - state.panX) * ratio;
    state.panY = py - (py - state.panY) * ratio;
    state.scale = next;
    applyView();
  }, { passive: false });

  const placeNodes = () => {
    for (const [nodeId, el] of nodeElements) {
      const pos = positionOf(nodeId);
      setStyles(el, `left:${pos.x}px;top:${pos.y}px;width:${layout.nodeWidth}px;min-height:${layout.nodeHeight}px`);
    }
  };

  for (const node of model.nodes) {
    const button = nodeLayer.createEl("button", {
      cls: `graph-canvas-node ${node.kindClass} ${node.statusClass}${node.isCurrent ? " is-current" : ""}${node.needsHuman ? " is-waiting" : ""}`,
    });
    button.setAttribute("type", "button");
    button.setAttribute("aria-label", `${node.label}（${node.statusLabel}）`);
    button.createSpan({ cls: `graph-canvas-node-marker kind-marker-${markerKindClass(node.kind)}`, text: KIND_MARKERS[node.kind] || "▢" });
    button.createSpan({ cls: "graph-canvas-node-label", text: node.label });
    const metaText = node.join
      ? `${node.kindLabel} · ${node.join.modeLabel} · ${node.statusLabel}`
      : `${node.kindLabel} · ${node.statusLabel}`;
    button.createSpan({ cls: "graph-canvas-node-meta", text: metaText });
    button.addEventListener("pointerdown", (event) => {
      const offset = sessionOffsets.get(node.nodeId) || { x: 0, y: 0 };
      beginDocDrag({ nodeId: node.nodeId, startX: event.clientX, startY: event.clientY, baseX: offset.x, baseY: offset.y, moved: false });
      event.stopPropagation?.();
    });
    button.addEventListener("click", () => {
      if (suppressClick) return;
      handlers.onSelectNode(node.nodeId);
    });
    nodeElements.set(node.nodeId, button);
  }

  drawEdges();
  placeNodes();
  fitView();

  const footer = container.createDiv({ cls: "graph-canvas-footer" });
  footer.createEl("span", {
    cls: "graph-canvas-legend",
    text: "实线=已走 · 虚线=未走 · 橙色=等待人工 · 红色=异常 · 绿色=已完成",
  });
  const actions = footer.createDiv({ cls: "graph-canvas-footer-actions" });
  const refresh = actions.createEl("button", { cls: "graph-canvas-refresh", text: "刷新" });
  refresh.setAttribute("type", "button");
  refresh.addEventListener("click", () => handlers.onRefresh());
  const fit = actions.createEl("button", { cls: "graph-canvas-fit", text: "适配视图" });
  fit.setAttribute("type", "button");
  fit.addEventListener("click", fitView);

  return { dispose, sessionOffsets };
}

module.exports = { renderGraphCanvas, KIND_MARKERS };
