"use strict";

const {
  computeConstellationPositions,
  computeRingLayout,
  computeRingCardStates,
  ringRotationForIndex,
  normalizeAngle,
} = require("./thought-map.js");

const ENTRY_BUTTON_CLASS = "life-loop-console-entry";
const ENTRY_BUTTON_LABEL = "Loop 控制台";
const THOUGHT_MAP_CLASS = "life-thought-map";
const EXPERIENCE_CONTROLS_CLASS = "life-experience-controls";
const EXPERIENCE_STORAGE_KEY = "my-life-homepage-reading-scale-v1";
const EXPERIENCE_SCALES = ["standard", "comfortable", "large"];
const DETAIL_HINT_TEXT = "点击卡片中的一条思考，查看它的判断与未知。";
const SVG_NS = "http://www.w3.org/2000/svg";
const TWEEN_DURATION_MS = 420;
const CRUISE_DEG_PER_MS = 0.006;

// Obsidian's DOM helpers (createEl/createDiv) don't cover SVG elements, so the
// mini constellation is built with createElementNS; test doubles without SVG
// support fall back to plain createElement.
function createSvgElement(doc, tag) {
  if (doc && typeof doc.createElementNS === "function") return doc.createElementNS(SVG_NS, tag);
  return doc.createElement(tag);
}

function addSvgClass(element, cls) {
  const names = String(cls).split(/\s+/).filter(Boolean);
  if (typeof element.addClass === "function") {
    for (const name of names) element.addClass(name);
  } else {
    element.setAttribute("class", `${element.getAttribute("class") || ""} ${names.join(" ")}`.trim());
  }
}

function setSvgSelected(element, selected) {
  if (element.classList) {
    element.classList.toggle("is-selected", selected);
  } else if (selected) {
    element.addClass("is-selected");
  } else {
    element.removeClass("is-selected");
  }
}

function injectHomepageEntry(doc, openConsole) {
  const matches = doc.querySelectorAll(".my-life-homepage-view .life-decision-bar .life-decision-match");
  let injected = 0;
  matches.forEach((container) => {
    if (container.querySelector(`.${ENTRY_BUTTON_CLASS}`)) return;
    const button = doc.createElement("button");
    button.setAttribute("type", "button");
    button.className = ENTRY_BUTTON_CLASS;
    button.textContent = ENTRY_BUTTON_LABEL;
    button.setAttribute("aria-label", "打开 Loop 控制台（只读）");
    button.addEventListener("click", () => openConsole());
    container.appendChild(button);
    injected += 1;
  });
  return injected;
}

function readingScaleStorage(storage) {
  if (storage) return storage;
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function storedReadingScale(storage) {
  try {
    const value = readingScaleStorage(storage)?.getItem(EXPERIENCE_STORAGE_KEY);
    return EXPERIENCE_SCALES.includes(value) ? value : "comfortable";
  } catch {
    return "comfortable";
  }
}

// The homepage Dataview block can be rebuilt at any time, so reading controls
// are injected per rendered copy and the preference lives outside that DOM.
// This is deliberately a display preference only: no Vault or plugin data is
// written, and repeated homepage scans remain idempotent.
function injectHomepageExperience(doc, storage) {
  const views = doc.querySelectorAll(".my-life-homepage-view");
  const store = readingScaleStorage(storage);
  let injected = 0;
  const applyScale = (scale) => {
    for (const view of doc.querySelectorAll(".my-life-homepage-view")) {
      view.setAttribute("data-life-reading-scale", scale);
      for (const button of view.querySelectorAll(`.${EXPERIENCE_CONTROLS_CLASS} button`)) {
        const selected = button.getAttribute("data-life-scale") === scale;
        button.setAttribute("aria-pressed", String(selected));
        button.toggleClass("is-active", selected);
      }
    }
  };

  const initialScale = storedReadingScale(store);
  views.forEach((view) => {
    view.setAttribute("data-life-reading-scale", initialScale);
    view.querySelector(".life-dashboard-content")?.addClass("life-motion-ready");
    const toolbar = view.querySelector(".life-view-switch") || view.querySelector(".life-command-actions");
    if (!toolbar || toolbar.querySelector(`.${EXPERIENCE_CONTROLS_CLASS}`)) return;
    const group = toolbar.createDiv({ cls: EXPERIENCE_CONTROLS_CLASS });
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "主页字号");
    const labels = { standard: "A", comfortable: "A+", large: "A++" };
    const descriptions = { standard: "标准字号", comfortable: "舒适字号", large: "大字号" };
    for (const scale of EXPERIENCE_SCALES) {
      const button = group.createEl("button", { text: labels[scale] });
      button.setAttribute("type", "button");
      button.setAttribute("data-life-scale", scale);
      button.setAttribute("aria-label", descriptions[scale]);
      button.setAttribute("title", descriptions[scale]);
      button.addEventListener("click", () => {
        try { store?.setItem(EXPERIENCE_STORAGE_KEY, scale); } catch {}
        applyScale(scale);
      });
    }
    injected += 1;
  });
  applyScale(initialScale);
  return injected;
}

function displayDate(value) {
  const raw = String(value || "");
  return /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : "时间待补充";
}

function renderThoughtDetail(panel, item, openNote) {
  panel.empty();
  panel.setAttribute("data-component-slots", item && item.home ? item.home.componentSlots.join(" ") : "");
  if (!item) {
    panel.addClass("is-hint");
    panel.createEl("p", { cls: "life-thought-detail-hint", text: DETAIL_HINT_TEXT });
    return;
  }
  panel.removeClass("is-hint");
  const meta = panel.createDiv({ cls: "life-thought-meta" });
  meta.createEl("span", { text: item.statusLabel });
  meta.createEl("time", { text: `更新于 ${displayDate(item.updatedAt)}` });
  // Package E：新式认知笔记带有结构化主页投影时，展示可信度、候选卡与
  // 未解问题计数，并把固定组件槽位暴露在 data hook 上；旧笔记无 home，
  // 渲染路径与之前完全一致。内容契约不依赖任何 CSS。
  if (item.home) {
    panel.setAttribute("data-component-slots", item.home.componentSlots.join(" "));
    meta.createEl("span", { text: `可信度 ${item.home.credibilityLabel}` });
    meta.createEl("span", { text: `候选知识卡 ${item.home.candidateCardCount}` });
    meta.createEl("span", { text: `未解问题 ${item.home.unresolvedQuestionCount}` });
  }
  panel.createEl("strong", { text: item.title });
  panel.createEl("p", { cls: "life-thought-judgment", text: item.summary });
  if (item.home && item.home.userValue) {
    panel.createEl("p", { cls: "life-thought-value", text: `对你的价值：${item.home.userValue}` });
  }
  if (item.unknowns.length) {
    panel.createEl("p", { cls: "life-thought-unknowns", text: `仍然未知：${item.unknowns.join("；")}` });
  }
  const button = panel.createEl("button", { cls: "life-thought-detail", text: "查看完整思考 →" });
  button.setAttribute("type", "button");
  button.addEventListener("click", () => openNote(item.path));
}

// Multiple rendered copies of My Life.md can coexist in the workspace, each
// with its own .life-dashboard-content. A fresh section is built per
// container — DOM nodes cannot be shared across copies, and each copy keeps
// its own ring and selection state.
function buildThoughtMapSection(doc, container, map, openNote) {
  const section = doc.createElement("section");
  section.className = `life-panel ${THOUGHT_MAP_CLASS}`;
  const heading = section.createDiv({ cls: "life-panel-heading" });
  const headingText = heading.createDiv();
  headingText.createEl("span", { cls: "life-source-label", text: "FRAGMENT → THINKING → OPEN QUESTIONS" });
  headingText.createEl("h2", { text: "思考地图" });
  heading.createEl("span", { cls: "life-panel-index", text: `${map.total} 条思考 · ${map.categories.length} 个主题` });
  section.createEl("p", { cls: "life-thought-intro", text: "把零散的思考转成一环主题卡片：转动圆环，每张卡片是一个主题，点一下转到正面，就能看到它的每一条思考。" });

  if (!map.total) {
    section.createEl("p", { cls: "life-empty-state", text: "记录一条碎片后，它会出现在这里。" });
    container.appendChild(section);
    return 1;
  }

  const legend = section.createDiv({ cls: "life-thought-legend" });
  const legendEntries = [
    ["open", `待展开 ${map.counts.open}`],
    ["provisional", `阶段判断 ${map.counts.provisional}`],
    ["concluded", `已有结论 ${map.counts.concluded}`],
  ];
  for (const [status, label] of legendEntries) {
    const count = map.counts[status];
    const entry = legend.createSpan({ cls: `life-thought-legend-item is-${status}${count ? "" : " is-empty"}` });
    entry.createSpan({ cls: "life-thought-legend-dot" });
    entry.createSpan({ text: label });
  }

  const layout = computeRingLayout(map.categories.length);
  const stage = section.createDiv({ cls: "life-thought-ring-stage" });
  stage.setAttribute("data-active-topic", "1");
  const stageCaption = stage.createDiv({ cls: "life-thought-stage-caption" });
  stageCaption.setAttribute("aria-live", "polite");
  stageCaption.createSpan({ cls: "life-thought-stage-eyebrow", text: "当前主题" });
  const stageCaptionTitle = stageCaption.createEl("strong", { text: map.categories[0].name });
  const ring = stage.createDiv({ cls: "life-thought-ring" });
  let cruiseButton = null;
  let previousButton = null;
  let nextButton = null;
  if (layout.count > 1) {
    const controls = section.createDiv({ cls: "life-thought-ring-controls" });
    previousButton = controls.createEl("button", { cls: "life-thought-ring-step", text: "← 上一个" });
    previousButton.setAttribute("type", "button");
    cruiseButton = controls.createEl("button", { cls: "life-thought-ring-cruise", text: "暂停巡航" });
    cruiseButton.setAttribute("type", "button");
    cruiseButton.setAttribute("aria-pressed", "true");
    nextButton = controls.createEl("button", { cls: "life-thought-ring-step", text: "下一个 →" });
    nextButton.setAttribute("type", "button");
    controls.createSpan({ cls: "life-thought-ring-hint", text: "悬停暂停 · 拖动旋转 · 点击卡片聚焦正面" });
  }
  const detailPanel = section.createDiv({ cls: "life-thought-detail-panel" });
  renderThoughtDetail(detailPanel, null, openNote);

  let rotation = 0;
  let tween = null; // { from, to, start, duration } — eased, time-based
  let activeIndex = 0;
  let selected = null; // { categoryIndex, itemIndex } | null
  const reducedMotion = typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let cruising = layout.count > 1 && !reducedMotion;
  let hovering = false;
  let dragging = false;
  let dragMoved = 0;
  let dragLastX = 0;
  let suppressClick = false;
  const cardElements = [];
  const cardRows = [];
  const cardStars = [];

  // Test doubles have no animation frame; there every state change snaps into
  // place synchronously so the ring stays fully testable under plain Node.
  const raf = !reducedMotion && typeof requestAnimationFrame === "function"
    ? requestAnimationFrame.bind(window)
    : null;

  // Theme colours follow the deterministic category order (zh-CN sort with
  // 未归类 last), so adjacent themes on the ring always get different hues;
  // 未归类 itself stays neutral grey.
  const hueClass = (category, categoryIndex) =>
    category.name === "未归类" ? "is-hue-none" : `is-hue-${categoryIndex % 8}`;

  const pauseCruise = () => {
    cruising = false;
    if (cruiseButton) {
      cruiseButton.setText("自动巡航");
      cruiseButton.setAttribute("aria-pressed", "false");
    }
  };

  // The ring element carries the global rotation plus a -radius translateZ
  // compensation, so the front card always lands on the z=0 plane (rendered
  // 1:1, sharp) while side and back cards recede; without it the front card
  // would scale up and rasterise blurry as the radius grows with more themes.
  // Each card only knows its base angle, radius, and depth-derived finish
  // (opacity/scale/z-index) — depth is painted with opacity, never a blur
  // filter, which is the expensive part of a 3D repaint.
  const applyStates = () => {
    const states = computeRingCardStates(map.categories.length, rotation);
    ring.setAttribute("style", `transform:translateZ(${-layout.radius}px) rotateY(${-rotation}deg)`);
    cardElements.forEach((card, index) => {
      const state = states[index];
      if (!state || !card) return;
      card.setAttribute(
        "style",
        `transform:rotateY(${index * layout.step}deg) translateZ(${layout.radius}px) scale(${state.scale.toFixed(3)});` +
        `opacity:${state.opacity.toFixed(3)};z-index:${state.zIndex}`
      );
    });
  };

  // The active highlight is a class swap on two elements — never a rebuild.
  const setActiveCard = (index) => {
    if (index === activeIndex) return;
    cardElements[activeIndex]?.removeClass("is-active");
    activeIndex = index;
    cardElements[activeIndex]?.addClass("is-active");
    stageCaptionTitle.setText(map.categories[activeIndex].name);
    stage.setAttribute("data-active-topic", String(activeIndex + 1));
  };

  // While the ring moves on its own (cruise or drag), the highlight follows
  // whichever face is at the front.
  const syncActiveToFront = () => {
    const front = computeRingCardStates(map.categories.length, rotation).find((state) => state.front);
    if (front) setActiveCard(front.index);
  };

  const startTween = (target) => {
    tween = { from: rotation, to: target, start: null, duration: TWEEN_DURATION_MS };
    if (!raf) {
      rotation = target;
      tween = null;
      applyStates();
    }
  };

  const focusCard = (index) => {
    if (index === activeIndex) return;
    // Focusing a card stops the cruise so the front card stays readable.
    pauseCruise();
    setActiveCard(index);
    startTween(ringRotationForIndex(index, map.categories.length, rotation));
  };

  const selectThought = (categoryIndex, itemIndex) => {
    // Reading a thought is an explicit interaction; the ring must stop moving
    // or the card would rotate away mid-read.
    pauseCruise();
    if (categoryIndex !== activeIndex) focusCard(categoryIndex);
    const alreadySelected = selected && selected.categoryIndex === categoryIndex && selected.itemIndex === itemIndex;
    if (selected) {
      cardRows[selected.categoryIndex]?.[selected.itemIndex]?.removeClass("is-selected");
      if (cardStars[selected.categoryIndex]?.[selected.itemIndex]) {
        setSvgSelected(cardStars[selected.categoryIndex][selected.itemIndex], false);
      }
    }
    if (alreadySelected) {
      selected = null;
      renderThoughtDetail(detailPanel, null, openNote);
      return;
    }
    selected = { categoryIndex, itemIndex };
    cardRows[categoryIndex]?.[itemIndex]?.addClass("is-selected");
    if (cardStars[categoryIndex]?.[itemIndex]) {
      setSvgSelected(cardStars[categoryIndex][itemIndex], true);
    }
    renderThoughtDetail(detailPanel, map.categories[categoryIndex].items[itemIndex], openNote);
  };

  // Every card is built exactly once with its full content — head, mini
  // constellation, and the complete thought list. From then on rotation only
  // rewrites inline transform/opacity styles and never touches the DOM tree:
  // no rebuild flashes, no homepage rescans, no lost selection state.
  map.categories.forEach((category, categoryIndex) => {
    const card = ring.createDiv({
      cls: `life-thought-ring-card ${hueClass(category, categoryIndex)}${categoryIndex === activeIndex ? " is-active" : ""}`,
    });
    card.setAttribute("aria-label", `切换到主题：${category.name}`);
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.addEventListener("click", () => {
      if (!suppressClick && categoryIndex !== activeIndex) focusCard(categoryIndex);
    });
    card.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && categoryIndex !== activeIndex) {
        event.preventDefault?.();
        focusCard(categoryIndex);
      }
    });
    cardRows[categoryIndex] = [];
    cardStars[categoryIndex] = [];

    const head = card.createDiv({ cls: "life-thought-card-head" });
    const headText = head.createDiv({ cls: "life-thought-card-heading" });
    headText.createSpan({ cls: "life-thought-card-heading-dot" });
    headText.createEl("h3", { cls: "life-thought-card-title", text: category.name });
    headText.createEl("span", { cls: "life-thought-card-count", text: `${category.items.length} 条思考` });

    const positions = computeConstellationPositions(category.items);
    const mini = createSvgElement(doc, "svg");
    mini.setAttribute("viewBox", "0 0 100 100");
    addSvgClass(mini, "life-thought-mini-map");
    head.appendChild(mini);
    if (positions.length >= 2) {
      const polyline = createSvgElement(doc, "polyline");
      polyline.setAttribute("points", positions.map((star) => `${star.x},${star.y}`).join(" "));
      addSvgClass(polyline, "life-thought-mini-link");
      mini.appendChild(polyline);
    }
    positions.forEach((position, index) => {
      const item = position.item;
      const circle = createSvgElement(doc, "circle");
      circle.setAttribute("cx", position.x);
      circle.setAttribute("cy", position.y);
      circle.setAttribute("r", item.rank >= 3 ? 7 : item.rank === 2 ? 5.5 : 4.5);
      circle.setAttribute("aria-label", `${item.title}（${item.statusLabel}）`);
      addSvgClass(circle, `life-thought-mini-star is-${item.status}`);
      circle.addEventListener("click", () => {
        if (!suppressClick) selectThought(categoryIndex, index);
      });
      mini.appendChild(circle);
      cardStars[categoryIndex][index] = circle;
    });

    const body = card.createDiv({ cls: "life-thought-card-body" });
    category.items.forEach((item, index) => {
      const row = body.createEl("button", { cls: `life-thought-row is-${item.status}` });
      row.setAttribute("type", "button");
      // Package E：新式认知笔记的行暴露稳定 data hook（schema、可信度、
      // 候选卡与未解问题计数），供后续 UI 整体替换；旧笔记没有 home，
      // 行结构与之前完全一致。
      if (item.home) {
        row.setAttribute("data-home-schema", item.home.schemaVersion);
        row.setAttribute("data-credibility", item.home.credibility);
        row.setAttribute("data-candidate-cards", String(item.home.candidateCardCount));
        row.setAttribute("data-unresolved", String(item.home.unresolvedQuestionCount));
      }
      row.createSpan({ cls: "life-thought-row-dot" });
      row.createSpan({ cls: "life-thought-row-title", text: item.title });
      if (item.unknowns.length) row.createSpan({ cls: "life-thought-row-unknown", text: "?" });
      row.createSpan({ cls: "life-thought-row-date", text: displayDate(item.updatedAt) });
      row.addEventListener("click", () => {
        if (!suppressClick) selectThought(categoryIndex, index);
      });
      cardRows[categoryIndex][index] = row;
    });

    cardElements[categoryIndex] = card;
  });
  applyStates();

  if (!cruising && cruiseButton) {
    cruiseButton.setText("自动巡航");
    cruiseButton.setAttribute("aria-pressed", "false");
  }

  previousButton?.addEventListener("click", () => {
    const index = (activeIndex - 1 + layout.count) % layout.count;
    focusCard(index);
  });
  nextButton?.addEventListener("click", () => {
    const index = (activeIndex + 1) % layout.count;
    focusCard(index);
  });

  if (cruiseButton) {
    cruiseButton.addEventListener("click", () => {
      cruising = !cruising;
      if (cruising) tween = null;
      cruiseButton.setText(cruising ? "暂停巡航" : "自动巡航");
      cruiseButton.setAttribute("aria-pressed", String(cruising));
    });
  }

  // Drag tracking lives on the document, and the stage NEVER calls
  // setPointerCapture: pointer capture retargets the compatibility click
  // event to the capturing element, which silently killed every click on
  // cards, thought rows, and the detail action under a real mouse.
  const dragDoc = stage.ownerDocument || doc;
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    if (dragDoc && typeof dragDoc.removeEventListener === "function") {
      dragDoc.removeEventListener("pointermove", onDragMove);
      dragDoc.removeEventListener("pointerup", endDrag);
      dragDoc.removeEventListener("pointercancel", endDrag);
    }
    if (dragMoved > 6) {
      suppressClick = true;
      if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
        window.setTimeout(() => { suppressClick = false; }, 0);
      } else {
        suppressClick = false;
      }
      // Snap to the nearest card so the ring never rests at an awkward angle.
      const front = computeRingCardStates(map.categories.length, rotation).find((state) => state.front);
      if (front) startTween(ringRotationForIndex(front.index, map.categories.length, rotation));
    }
  };
  const onDragMove = (event) => {
    if (!dragging) return;
    const x = Number(event && event.clientX) || 0;
    const dx = x - dragLastX;
    dragLastX = x;
    dragMoved += Math.abs(dx);
    rotation = normalizeAngle(rotation - dx * 0.35);
    applyStates();
    syncActiveToFront();
  };
  stage.addEventListener("pointerdown", (event) => {
    if (layout.count < 2) return;
    dragging = true;
    dragMoved = 0;
    dragLastX = Number(event && event.clientX) || 0;
    tween = null;
    if (dragDoc && typeof dragDoc.addEventListener === "function") {
      dragDoc.addEventListener("pointermove", onDragMove);
      dragDoc.addEventListener("pointerup", endDrag);
      dragDoc.addEventListener("pointercancel", endDrag);
    }
  });
  // Hovering pauses the cruise (without touching the explicit cruise toggle):
  // a moving card is impossible to read or click, and a press-release pair
  // straddling a rotation kills the click event before it can fire.
  stage.addEventListener("pointerenter", () => { hovering = true; });
  stage.addEventListener("pointerleave", () => { hovering = false; });

  // Spatial rotation lives in JS: an eased time-based tween after
  // clicks/drag-snap, or a slow idle cruise. CSS only handles surface feedback.
  if (raf) {
    let lastFrame = 0;
    const tick = (now) => {
      if (typeof ring.isConnected === "boolean" && !ring.isConnected) return; // section removed
      const dt = Math.min(64, Math.max(0, now - (lastFrame || now)));
      lastFrame = now;
      if (!dragging) {
        if (tween) {
          if (tween.start === null) tween.start = now;
          const progress = Math.min(1, (now - tween.start) / tween.duration);
          const eased = 1 - Math.pow(1 - progress, 3);
          rotation = tween.from + (tween.to - tween.from) * eased;
          if (progress >= 1) {
            rotation = tween.to;
            tween = null;
          }
          applyStates();
        } else if (cruising && !hovering && layout.count > 1) {
          rotation = normalizeAngle(rotation + dt * CRUISE_DEG_PER_MS);
          applyStates();
          syncActiveToFront();
        }
      }
      raf(tick);
    };
    raf(tick);
  }

  const fragments = container.querySelector(".life-fragments");
  if (fragments && typeof fragments.insertAdjacentElement === "function") {
    fragments.insertAdjacentElement("afterend", section);
  } else {
    container.appendChild(section);
  }
  return 1;
}

// Inject one section into every homepage copy that does not have one yet;
// copies already carrying a section are skipped, so the scan stays idempotent
// per container. Returns the number of sections injected (0..N).
function injectHomepageThoughtMap(doc, map, openNote) {
  const containers = doc.querySelectorAll(".my-life-homepage-view .life-dashboard-content");
  let injected = 0;
  containers.forEach((container) => {
    if (container.querySelector(`.${THOUGHT_MAP_CLASS}`)) return;
    injected += buildThoughtMapSection(doc, container, map, openNote);
  });
  return injected;
}

module.exports = {
  ENTRY_BUTTON_CLASS,
  ENTRY_BUTTON_LABEL,
  THOUGHT_MAP_CLASS,
  EXPERIENCE_CONTROLS_CLASS,
  EXPERIENCE_STORAGE_KEY,
  injectHomepageEntry,
  injectHomepageExperience,
  injectHomepageThoughtMap,
};
