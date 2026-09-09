// Cosmos UI Interaction Fix V1 — 用户可见结果级验收（TASK-COSMOS-UI-INTERACTION-FIX-V1 §3 契约）。
// 断言级别：反馈文字出现 / DOM 状态变化 / 视图打开——adapter 被调不算通过。
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, existsSync, readdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

let requestUrlHandler = async () => ({ status: 200, text: JSON.stringify({ contract_version: '2', data: [] }) });
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
      requestUrl: (options) => requestUrlHandler(options),
      ItemView: class {
        constructor() {
          this.contentEl = new FakeEl('div');
          this.app = {};
        }
      },
    };
  }
  return originalLoad.call(this, request, ...rest);
};
const { setupLoopConsole, createRepairRequestCapability } = require(path.join(ROOT, 'src', 'console', 'register.js'));
Module._load = originalLoad;

// P0-1（R25）：隔离即保护——harness 默认注入临时消费目录，测试代码不再
// 触达真实看门狗目录。2026-08-27 起按 nigo 指令：测试套件连真实目录的
// 读取（快照/比对）也不做；证据 = 既有污染记录 + 注入后写入只落临时目录。
// 每个插件实例默认隔离到独立的临时消费目录。
function isolatedConsumerDirs() {
  return {
    authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
    repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
  };
}

function envelope(data) {
  return { status: 200, text: JSON.stringify({ contract_version: '2', data }) };
}

class FakeMutationObserver {
  static instances = [];
  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
    FakeMutationObserver.instances.push(this);
  }
  observe() {}
  disconnect() { this.disconnected = true; }
}

function makeWindow() {
  const win = {
    setTimeout: (fn) => ({ fn }),
    clearTimeout: () => {},
    setInterval: () => ({}),
    clearInterval: () => {},
    requestAnimationFrame: () => ({}),
    cancelAnimationFrame: () => {},
    matchMedia: () => ({ matches: true }),
    confirm: () => true,
  };
  return win;
}

class FakeDocument extends FakeEl {
  constructor() {
    super('document');
    this.activeElement = null;
  }
}

function makeDocument() {
  const doc = new FakeDocument();
  doc.body = doc.createDiv({ cls: 'body' });
  return doc;
}

async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function byAction(root, actionId) {
  return [...root.querySelectorAll('[data-cosmos-action]')]
    .find((el) => el.getAttribute('data-cosmos-action') === actionId) || null;
}
function byText(root, text) {
  return [...root.querySelectorAll('button')].find((el) => el.textContent === text) || null;
}

describe('Cosmos Interaction Fix V1 — 用户可见结果级', () => {
  let saved;

  beforeEach(() => {
    FakeMutationObserver.instances = [];
    saved = {
      document: globalThis.document,
      window: globalThis.window,
      MutationObserver: globalThis.MutationObserver,
    };
    globalThis.window = makeWindow();
    globalThis.MutationObserver = FakeMutationObserver;
    requestUrlHandler = async () => envelope([]);
  });

  afterEach(() => {
    globalThis.document = saved.document;
    globalThis.window = saved.window;
    globalThis.MutationObserver = saved.MutationObserver;
  });

  function buildRoot(doc) {
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const root = home.createDiv({ cls: 'life-home' });
    const mount = root.createDiv({ cls: 'life-cosmos-mount' });
    mount.setAttribute('data-life-cosmos-mount', '1');
    const legacy = root.createDiv({ cls: 'life-cosmos-legacy' });
    const dashboard = legacy.createDiv({ cls: 'life-dashboard-content' });
    // 盲区 fixture
    const blindspots = dashboard.createEl('section', { cls: 'life-blindspots' });
    const li = blindspots.createEl('ul').createEl('li');
    li.createSpan({ text: '有未读的今日资料' });
    li.createEl('button', { text: '只看未读' }).setAttribute('data-view-filter', 'unread');
    // focus bar fixture（有焦点）
    const focusBar = dashboard.createEl('section', { cls: 'life-focus-bar' });
    focusBar.createEl('strong', { text: '当前专注文档' });
    focusBar.createDiv({ cls: 'life-focus-time', text: '25:00' });
    focusBar.createEl('small', { text: '打开文档后启动阅读计时' });
    // P1-3/F-K13：逾期项带显式 path（失效路径由 vaultMisses 注入控制）
    const attention = dashboard.createEl('section', { cls: 'life-attention' });
    const overdueLi = attention.createEl('ul').createEl('li');
    overdueLi.createSpan({ text: '测试逾期任务' });
    const overdueSmall = overdueLi.createEl('small', { text: '截止 08-12' });
    const overdueLink = overdueSmall.createEl('a', { text: '来源' });
    overdueLink.setAttribute('data-path', 'tasks/missing.md');
    // 决策卡 fixture（P1-2 卡堆 + F-D5 搜索）
    const intentCard = dashboard.createEl('div', { cls: 'fragment-intent-card' });
    intentCard.createEl('strong', { text: '待确认方向甲' });
    intentCard.createEl('p', { text: '证据仍不足' });
    intentCard.createEl('button', { cls: 'fragment-intent-confirm', text: '确认并继续' });
    // daily 信号卡 fixture（F-D5/F-D1 消费面：is-read + data-path）
    const dailyCard = dashboard.createEl('article', { cls: 'life-daily-card is-read' });
    const dailyLink = dailyCard.createEl('a', { text: '今日综合日报' });
    dailyLink.setAttribute('data-path', 'daily/2026-08-22.md');
    dailyCard.createEl('small', { text: '08:00 自动生成' });
    return { home, root, mount, legacy, dashboard };
  }

  async function setupMounted({ vaultMisses = [], beforeMount } = {}) {
    const doc = makeDocument();
    globalThis.document = doc;
    const pluginCalls = [];
    const openedLinks = [];
    const plugin = {
      registerView() {},
      addCommand() {},
      register() {},
      registerEvent() {},
      captureHomeScroll() {},
      openQuickCapture: async () => {},
      openContentManager: async () => {},
      toggleReadingTimer: async () => { pluginCalls.push(['toggleReadingTimer']); },
      resetCurrentReadingTime: async () => { pluginCalls.push(['resetCurrentReadingTime']); },
      setViewPreference: async (key, value) => { pluginCalls.push(['setViewPreference', key, value]); },
      getThoughtMap: async () => ({ total: 0, counts: {}, categories: [{ name: '主题一', items: [] }] }),
      getViewPreferences: () => ({ filter: 'all', density: 'comfortable', ...isolatedConsumerDirs() }),
      app: {
        commands: { executeCommandById: async () => {} },
        workspace: {
          on: () => ({}),
          onLayoutReady: () => {},
          getLeavesOfType: () => [],
          openLinkText: async (p) => { openedLinks.push(p); },
        },
        vault: {
          getAbstractFileByPath: (ref) => (ref && !vaultMisses.includes(ref) ? { extension: 'md', path: ref } : null),
          read: async () => '',
        },
        metadataCache: { getFileCache: () => ({ frontmatter: {} }) },
      },
    };
    setupLoopConsole(plugin);
    await settle();
    const current = buildRoot(doc);
    if (beforeMount) beforeMount(current);
    const handshake = await plugin.mountHomepageCosmos(current.root);
    await settle();
    return { doc, plugin, pluginCalls, openedLinks, current, handshake };
  }

  /* F-D1：盲区筛选 → capsule 反馈出现 + 过滤效果真实翻转（P2-1 钉住） */
  it('F-D1: 点击盲区动作后反馈出现、持久写转发、已读信号行 hidden 真实翻转', async () => {
    const { pluginCalls, current, doc, handshake } = await setupMounted();
    assert.equal(handshake, 'mounted', `握手应成功，got ${handshake}`);
    const cosmosNodes = doc.querySelectorAll('.life-cosmos-home').length;
    assert.ok(cosmosNodes > 0, `Cosmos 已挂载，got ${cosmosNodes}`);
    const btn = byAction(current.mount, 'view.setFilter');
    assert.ok(btn, '盲区动作按钮存在');
    // fixture 的 daily 卡 is-read → 投影为带 data-read 的信号行
    // （FakeEl matches 只支持 [attr] 存在性，按值过滤用 find）
    const readRow = [...current.mount.querySelectorAll('.life-cb-signal-row')]
      .find((el) => el.getAttribute('data-read') === '1');
    assert.ok(readRow, 'fixture 应产生一条已读信号行（过滤对象）');
    assert.equal(readRow.getAttribute('hidden'), null, '过滤前该行可见');
    btn.click();
    await settle(100);
    const status = current.mount.querySelector('.life-cosmos-capsule-status');
    assert.ok(status && status.text.length > 0, `capsule 反馈非空，got "${status?.text}"`);
    assert.equal(status.getAttribute('data-kind'), 'success');
    assert.ok(status.text.includes('1 条信号已同步过滤'), `say 文案含实际受影响数，got "${status.text}"`);
    // P2-1 核心：hidden 状态真实翻转——选择器/过滤逻辑回归时此断言变红
    assert.equal(readRow.getAttribute('hidden'), 'hidden', '已读信号行过滤后必须隐藏');
    assert.ok(pluginCalls.some((c) => c[0] === 'setViewPreference' && c[1] === 'filter'), '持久写走既有 handler');
  });

  /* F-D2：focus.toggle 按钮文案翻转 */
  it('F-D2: toggle 成功后按钮文案翻转且状态行更新', async () => {
    const { current } = await setupMounted();
    const go = byAction(current.mount, 'focus.toggle');
    assert.ok(go, 'focus.toggle 存在（有焦点 fixture）');
    const before = go.textContent;
    go.click();
    await settle(100);
    assert.notEqual(go.textContent, before, `文案应翻转 ${before} → ${go.textContent}`);
    const stateLine = current.mount.querySelector('.life-cb-t-state');
    if (stateLine) {
      const flipped = before === '启动记录'
        ? stateLine.text.includes('正在记录')
        : stateLine.text.includes('未在计时');
      assert.ok(flipped, `状态行同步翻转，got "${stateLine.text}"`);
    }
  });

  /* F-D2：reset 确认门 */
  it('F-D2: reset 取消确认零副作用，确认后才清零', async () => {
    const { pluginCalls, current } = await setupMounted();
    const reset = byAction(current.mount, 'focus.reset');
    assert.ok(reset, 'reset 按钮存在');
    globalThis.window.confirm = () => false;
    reset.click();
    await settle(100);
    assert.ok(!pluginCalls.some((c) => c[0] === 'resetCurrentReadingTime'), '取消确认不调用清零');
    globalThis.window.confirm = () => true;
    reset.click();
    await settle(100);
    assert.ok(pluginCalls.some((c) => c[0] === 'resetCurrentReadingTime'), '确认后调用清零');
  });

  /* P1-3/F-K13：失效路径不打开 + 可见错误；有效路径正常打开（对照） */
  it('F-K13: 失效路径 fail closed 且有可见错误反馈', async () => {
    const { openedLinks, current, doc } = await setupMounted({ vaultMisses: ['tasks/missing.md'] });
    // 逾期项"打开来源"是 mount 内第一个 item.openNote 按钮
    const overdueOpen = byAction(current.mount, 'item.openNote');
    assert.ok(overdueOpen, '逾期项打开按钮存在');
    overdueOpen.click();
    await settle(150);
    assert.equal(openedLinks.filter((p) => p === 'tasks/missing.md').length, 0, '失效路径不得打开');
    // 可见错误反馈出现在 capsule status
    const status = current.mount.querySelector('.life-cosmos-capsule-status');
    assert.ok(status && status.text.includes('未找到文档'), `可见错误反馈，got "${status?.text}"`);
    assert.equal(status.getAttribute('data-kind'), 'error');
  });

  it('F-K13 对照: 有效路径正常打开笔记', async () => {
    const { openedLinks, current } = await setupMounted({});
    const overdueOpen = byAction(current.mount, 'item.openNote');
    assert.ok(overdueOpen);
    overdueOpen.click();
    await settle(150);
    assert.deepEqual(openedLinks, ['tasks/missing.md'], '有效路径真实打开');
  });

  /* P1-2：卡堆全部稍后 → 恢复入口可见 + 文案区分 */
  it('P1-2: 全部稍后时撤销入口仍可见，空态文案区分', async () => {
    const { current } = await setupMounted();
    // fixture 有 1 张决策卡（fragment-intent-card）
    const snooze = [...current.mount.querySelectorAll('button')].find((b) => b.textContent === '稍后');
    assert.ok(snooze, '稍后按钮存在');
    snooze.click();
    await settle(100);
    const undo = byAction(current.mount, 'deck.undoSnooze');
    assert.ok(undo, '撤销按钮存在');
    assert.ok(undo.text.includes('全部'), `全部稍后态文案区分，got "${undo.text}"`);
    const emptyTitle = current.mount.querySelector('.life-cosmos-deck-empty strong');
    assert.ok(emptyTitle.text.includes('并未处理完'), `空态文案诚实，got "${emptyTitle.text}"`);
    undo.click();
    await settle(100);
    assert.ok(!undo.text.includes('全部'), '收回后回到部分稍后态');
  });

  /* P1-4/F-D5：搜索覆盖扩充 + aria-label 声明 */
  it('局部搜索只筛选研究记录，不隐藏需要确认的操作卡', async () => {
    const { current } = await setupMounted({ beforeMount(fixture) {
      const card = fixture.dashboard.createEl('article', { cls: 'life-capture-card' });
      card.setAttribute('data-fragment-id', 'search-fragment');
      card.createEl('strong', { text: '用于搜索的碎片' });
    }});
    assert.equal(current.mount.querySelector('.life-cosmos-capsule-search'), null, '不再提供覆盖范围不完整的全局搜索');
    const input = current.mount.querySelector('.life-frag-panorama-search');
    const row = current.mount.querySelector('.life-frag-panorama-row');
    const deckCard = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find(el => !el.classes.has('is-offline'));
    input.value = '完全不相关的词';
    input.dispatchEvent({ type: 'input', target: input });
    assert.equal(row.getAttribute('hidden'), 'hidden');
    assert.equal(deckCard.getAttribute('hidden'), null, '局部搜索不会吞掉确认操作');
    input.value = '用于搜索';
    input.dispatchEvent({ type: 'input', target: input });
    assert.equal(row.getAttribute('hidden'), null);
  });

  /* P1-4/F-K5：主板刷新失败 say 可见 */
  it('F-K5: 主板 graph.refresh 失败反馈可见', async () => {
    requestUrlHandler = async () => ({ status: 500, text: 'server error' });
    const { current } = await setupMounted();
    const refreshBtn = byAction(current.mount, 'graph.refresh');
    assert.ok(refreshBtn, '主板刷新按钮存在');
    refreshBtn.click();
    await settle(150);
    const status = current.mount.querySelector('.life-cosmos-capsule-status');
    assert.ok(status, 'capsule status 存在');
    assert.notEqual(status.getAttribute('data-kind'), null, '失败后有反馈（kind 标注）');
    requestUrlHandler = async () => envelope([]);
  });

  /* P1-4/F-D4：星历无占位条目（activity 为空不点亮、不可点） */
  it('F-D4: 星历空活动日不再制造假 affordance', async () => {
    const { current } = await setupMounted();
    // fixture activity 为空 → 所有 day cell 无 has 类且不可点出条目
    const cells = [...current.mount.querySelectorAll('.life-cb-day')].filter((c) => c.classes.has('has'));
    assert.equal(cells.length, 0, `activity 空时不点亮任何日期格，got ${cells.length}`);
  });

  /* P1-4/F-K6：盲区标题行非交互 */
  it('F-K6: 盲区标题行标记为非交互', async () => {
    const { current } = await setupMounted();
    const titleSpan = current.mount.querySelector('.life-cosmos-att-title[data-noninteractive]');
    assert.ok(titleSpan, '盲区标题带 data-noninteractive 标记');
    assert.equal(titleSpan.tagName, 'SPAN', '标题是 span 不是 button');
  });

  /* P1-4/F-D9：占位文案不进详情 rows——通过 showElement 路径验证。
     构造含占位 small 的资产卡，reveal 后 drawer 正文不含该文案。 */
  it('F-D9: 详情 rows 不含已知占位文案', async () => {
    const { current, doc } = await setupMounted();
    // 直接构造 drawer 场景：用 fragment 卡 reveal
    const go = [...current.mount.querySelectorAll('button')].find((b) => b.textContent === '去拍板 →');
    assert.ok(go, '去拍板按钮存在');
    go.click();
    await settle(150);
    const drawerBody = current.mount.querySelector('.life-cosmos-drawer-body');
    if (drawerBody) {
      for (const banned of ['暂无可打开文档', '打开查看详情', '只读统计']) {
        const rows = [...drawerBody.querySelectorAll('p')].map((p) => p.text);
        const leaked = rows.filter((t) => t.includes(banned) && !rows[0]?.includes('说明'));
        // 占位文案只能出现在显式说明行（"这里暂时没有更多说明。"），不得作为详情内容
        assert.ok(!leaked.some((t) => t.trim() === banned), `占位文案"${banned}"不得作为详情行`);
      }
    }
  });
  /* R25 P1-9：Cosmos 文档打开入口统一走 guarded item.openNote capability——
     失败可见（drawer 保持 + 行内错误 / 板块内 note），只有成功才关闭抽屉。 */
  describe('R25 P1-9 打开文档入口守卫', () => {
  const addCaptureCard = (fixture, rawPath) => {
    const card = fixture.dashboard.createEl('article', { cls: 'life-capture-card' });
    card.createEl('strong', { text: '守卫测试碎片' });
    card.createEl('p', { text: '带显式路径的碎片' });
    card.setAttribute('data-fragment-id', rawPath.split('/').pop().replace(/\.md$/, ''));
    const link = card.createEl('a', { text: '原始记录' });
    link.setAttribute('data-path', rawPath);
    return card;
  };
  const openFragmentDrawer = async (current) => {
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row, '带稳定身份的研究记录存在');
    row.click();
    await settle();
    return current.mount.querySelector('.life-cosmos-drawer');
  };

  it('P1-9 missing path → 抽屉保持 + 行内错误可见（不静默、不先关后败）', async () => {
    const { current } = await setupMounted({
      vaultMisses: ['散记/碎片想法/fragment-x.md'],
      beforeMount: (fixture) => { addCaptureCard(fixture, '散记/碎片想法/fragment-x.md'); },
    });
    const drawer = await openFragmentDrawer(current);
    const openBtn = [...drawer.querySelectorAll('button')].find((b) => (b.textContent || '') === '打开原始记录');
    assert.ok(openBtn, '打开原始记录按钮存在');
    openBtn.click();
    await settle();
    const shell = current.mount.querySelector('.life-cosmos-drawer-shell');
    assert.ok(shell && shell.classes.has('is-open'), '失败时抽屉保持打开');
    const line = drawer.querySelector('.life-cosmos-drawer-message');
    assert.ok(line && (line.textContent || '').includes('未找到文档'), '行内错误可见');
  });

  it('P1-9 成功打开 → 抽屉关闭 + 防重复（连点只调一次 openLinkText）', async () => {
    const { current, openedLinks } = await setupMounted({
      beforeMount: (fixture) => { addCaptureCard(fixture, '散记/碎片想法/fragment-ok.md'); },
    });
    const drawer = await openFragmentDrawer(current);
    const openBtn = [...drawer.querySelectorAll('button')].find((b) => (b.textContent || '') === '打开原始记录');
    assert.ok(openBtn, '打开原始记录按钮存在');
    openBtn.click();
    openBtn.click(); // 防重：actionButton disabled 守卫
    await settle();
    assert.deepEqual(openedLinks, ['散记/碎片想法/fragment-ok.md'], '成功打开恰好一次');
    const shell = current.mount.querySelector('.life-cosmos-drawer-shell');
    assert.ok(shell && !shell.classes.has('is-open'), '成功后抽屉关闭');
  });

  it('P1-9 目录行打开失败 → 观测卡 note 可见错误（不静默）', async () => {
    const { current } = await setupMounted({
      vaultMisses: ['资产/缺失.md'],
      beforeMount: (fixture) => {
        const card = fixture.dashboard.createEl('article', { cls: 'life-asset-card' });
        card.setAttribute('data-path', '资产/缺失.md');
        card.createEl('strong', { text: '守卫测试资产' });
      },
    });
    [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((b) => b.textContent === '知识库').click();
    await settle();
    const row = current.mount.querySelector('.life-cb-cat-row');
    assert.ok(row, '目录行存在');
    row.click();
    await settle();
    const note = current.mount.querySelector('.life-cb-scope-note');
    assert.ok(note && (note.textContent || '').includes('未找到文档'), '失败原因在观测卡可见');
  });
  });

  /* R26 P2-1 键盘可达性（gate_03b0060f2b4d 批准范围：路径链接、目录行、
     一个 dblclick-only 详情入口；原生不可用时 role/tabindex/Enter/Space）。 */
  describe('R26 P2-1 键盘可达性', () => {
    const addCaptureCard = (fixture, rawPath) => {
      const card = fixture.dashboard.createEl('article', { cls: 'life-capture-card' });
      card.createEl('strong', { text: '键盘测试碎片' });
      card.createEl('p', { text: '带显式路径的碎片' });
      card.setAttribute('data-fragment-id', rawPath.split('/').pop().replace(/\.md$/, ''));
    const link = card.createEl('a', { text: '原始记录' });
      link.setAttribute('data-path', rawPath);
      return card;
    };
    // 返回 preventDefault 是否被调用（R27 P1-4：keydown 必须抑制合成 click）。
    const keydownEnter = (el) => keydown(el, 'Enter');
    const keydown = (el, key) => {
      const handlers = el.listeners && el.listeners.keydown;
      assert.ok(handlers && handlers.length, '有 keydown 入口');
      let prevented = false;
      handlers[0]({ key, preventDefault() { prevented = true; }, stopPropagation() {} });
      return prevented;
    };

    it('路径链接：role=link + tabindex=0 + Enter 打开（键盘等价 click）', async () => {
      const { current, openedLinks } = await setupMounted({
        beforeMount: (fixture) => {
          const card = fixture.dashboard.createEl('article', { cls: 'life-organized-card' });
          card.createEl('strong', { text: '整理卡' });
          card.createEl('p', { text: '目标 核对 notes/a.md 的表述' });
        },
      });
      [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((b) => b.textContent === '研究记录').click();
      await settle();
      const link = current.mount.querySelector('.life-cb-path-link');
      assert.ok(link, '路径链接存在');
      assert.equal(link.getAttribute('role'), 'link', 'role=link');
      assert.equal(link.getAttribute('tabindex'), '0', 'tabindex=0 可聚焦');
      keydownEnter(link);
      await settle();
      assert.deepEqual(openedLinks, ['notes/a.md'], 'Enter 打开同一守卫路径');
    });

    it('目录行：role=button + tabindex=0 + focus 等价预览 + Enter 打开', async () => {
      const { current, openedLinks } = await setupMounted({
        beforeMount: (fixture) => {
          for (const [title, p] of [['资产甲', '资产/甲.md'], ['资产乙', '资产/乙.md']]) {
            const card = fixture.dashboard.createEl('article', { cls: 'life-asset-card' });
            card.setAttribute('data-path', p);
            card.createEl('strong', { text: title });
          }
        },
      });
      [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((b) => b.textContent === '知识库').click();
      await settle();
      const rows = [...current.mount.querySelectorAll('.life-cb-cat-row')];
      assert.equal(rows.length, 2, '两行目录');
      assert.equal(rows[1].getAttribute('role'), 'button', 'role=button');
      assert.equal(rows[1].getAttribute('tabindex'), '0', 'tabindex=0');
      assert.ok(!rows[1].classes.has('hot'), '第二行初始未激活');
      rows[1].listeners.focus[0]({});
      assert.ok(rows[1].classes.has('hot'), 'focus 等价 pointerenter 激活预览');
      keydownEnter(rows[1]);
      await settle();
      assert.deepEqual(openedLinks, ['资产/乙.md'], 'Enter 打开聚焦行的守卫路径');
    });

    it('研究记录使用原生按钮一次激活即可打开详情，不需锁定或双击', async () => {
      const { current } = await setupMounted({
        beforeMount: (fixture) => { addCaptureCard(fixture, '散记/碎片想法/fragment-kb.md'); },
      });
      const row = current.mount.querySelector('.life-frag-panorama-row');
      assert.ok(row);
      assert.equal(row.tagName, 'BUTTON', '原生按钮提供键盘激活语义');
      assert.equal(current.mount.querySelector('.life-cosmos-rock'), null, '不再需要二次锁定星球');
      row.click(); // FakeEl 模拟原生按钮激活；真实 Enter 路径由宿主验收。
      await settle();
      const shell = current.mount.querySelector('.life-cosmos-drawer-shell');
      assert.ok(shell.classes.has('is-open'));
      assert.ok(current.mount.querySelector('.life-cosmos-drawer').text.includes('键盘测试碎片'));
    });
  });
});

/* P1-4/F-D8：canvas 键盘无悬停不再落 systems[0]——源码级断言
   （mount 套件对关键契约同样使用源码静态断言先例）。 */
it('F-D8: canvas keydown 无悬停时传 null category 而非 systems[0]', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(path.join(ROOT, 'src', 'console', 'homepage-cosmos.js'), 'utf8');
  assert.ok(!src.includes('systems[Math.max(0, hoverSys)]'), '不得再回落到 systems[0]');
  assert.ok(src.includes('const category = hoverSys >= 0 ? systems[hoverSys]?.category : null;'), '无悬停传 null → 总览形态');
});

/* R26 P1-2：repair.request 非法 target 在 adapter 边界确定性拒绝——不增加
   产品入口（UI 仍固定 target=n8n），直接对既有 harness 导入的 capability
   工厂断言：结构化 error、零文件写入、绝不假成功；成功对照保留。 */
it('R26 P1-2: 非法 target → 结构化 error + 零写入；合法 target 成功对照', async () => {
  const tmpParent = mkdtempSync(path.join(os.tmpdir(), 'p12-repair-'));
  const repairDir = path.join(tmpParent, 'repair-requests');
  try {
    const plugin = { getViewPreferences: () => ({ repairRequestDir: repairDir }) };
    const repair = createRepairRequestCapability(plugin);
    for (const badPayload of [{ target: 'feishu' }, {}, null]) {
      const result = await repair(badPayload);
      assert.equal(result.kind, 'error', '非法 target 返回结构化 error');
      assert.ok(result.message.includes('仅支持 n8n'), '错误文案为冻结契约文案');
      assert.ok(!existsSync(repairDir), '非法 target 零文件写入（目录都未创建）');
    }
    const ok = await repair({ target: 'n8n' });
    assert.equal(ok.kind, 'success', '合法 target 成功对照');
    assert.equal(readdirSync(repairDir).filter((f) => f.startsWith('repair-')).length, 1, '成功恰好写一份请求文件');
  } finally {
    const { rmSync } = await import('node:fs');
    rmSync(tmpParent, { recursive: true, force: true });
  }
});
