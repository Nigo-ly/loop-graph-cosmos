import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';
import { existsSync, readFileSync, writeFileSync, mkdtempSync, rmSync, readdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
const require = createRequire(import.meta.url);
const Module = require('node:module');
const originalLoad = Module._load;
let engine; let fragment; let markdownCalls; let components;
Module._load = function(name, ...rest) {
  if (name === 'obsidian') return {
    ItemView: class {}, requestUrl() {},
    Component: class { constructor() { this.unloads = 0; components.push(this); } load() {} unload() { this.unloads++; } },
    MarkdownRenderer: { render: async (_app, text, host) => { markdownCalls.push(text); host.createEl('p', { text }); } },
    loadMermaid: async () => engine,
    sanitizeHTMLToDom: () => fragment,
  };
  return originalLoad.call(this, name, ...rest);
};
const { renderKnowledgeMarkdown } = require('../src/console/register.js');
Module._load = originalLoad;
FakeEl.prototype.getAttributeNames = function() { return Object.keys(this.attributes); };
function setup() {
  const doc = new FakeEl('document'); doc.body = doc.createDiv(); globalThis.document = doc;
  const target = doc.body.createDiv();
  return { doc, target };
}
function svgFragment() {
  const tree = new FakeEl('div'); const svg = tree.createEl('svg');
  svg.setAttribute('viewBox', '0 0 300 140');
  const shape = svg.createEl('rect'); shape.setAttribute('width', '100');
  svg.createEl('text', { text: '提出问题 → 核对证据' });
  return tree;
}
const flow = 'flowchart LR\nA[提出问题] --> B[核对证据]';
function render(target, body = flow) { return renderKnowledgeMarkdown({}, '结论结构\n```mermaid\n' + body + '\n```\n限制仍在', target, '研究.md'); }

describe('built-in strict Mermaid to static knowledge SVG', () => {
  beforeEach(() => {
    markdownCalls = []; components = []; fragment = svgFragment();
    engine = { mermaidAPI: { getConfig: () => ({ securityLevel: 'strict' }) },
      initialize() { throw new Error('global config must not change'); },
      render: async () => ({ svg: '<native-svg-fixture>', bindFunctions() { throw new Error('interactive binding forbidden'); } }),
    };
  });
  it('uses only the installed strict engine, per-diagram text labels, and no native trust-gated Markdown fence', async () => {
    const { doc, target } = setup(); const rendered = [];
    engine.render = async (...args) => { rendered.push(args); return { svg: '<native-svg-fixture>' }; };
    const operation = render(target); await operation.finished;
    assert.equal(rendered.length, 1);
    assert.match(rendered[0][1], /htmlLabels: false/);
    assert.ok(rendered[0][1].endsWith(flow));
    assert.equal(markdownCalls.some(text => text.includes('```mermaid')), false);
    assert.match(target.querySelector('svg').text, /核对证据/);
    assert.equal(target.querySelector('svg').getAttribute('role'), 'img');
    assert.equal(doc.querySelector('.life-knowledge-flow-measure'), null);
    operation.dispose(); assert.equal(components[0].unloads, 1);
  });
  it('rejects active output even if the host sanitizer leaves it behind', async () => {
    const { target } = setup(); const svg = fragment.querySelector('svg');
    for (const tag of ['script', 'foreignObject', 'image', 'a', 'style', 'iframe', 'animate']) svg.createEl(tag, { text: 'must be removed' });
    svg.setAttribute('class', 'internal-link');
    svg.setAttribute('onload', 'alert(1)'); svg.setAttribute('style', 'background:url(https://example.org)');
    svg.setAttribute('href', 'javascript:alert(1)');
    const edge = svg.createEl('path'); edge.setAttribute('d', 'M0,0L10,10'); edge.setAttribute('marker-end', 'url(https://example.org/arrow)');
    const safeEdge = svg.createEl('path'); safeEdge.setAttribute('marker-end', 'url(#local-arrow)');
    const operation = render(target); await operation.finished;
    for (const tag of ['script', 'foreignObject', 'image', 'a', 'style', 'iframe', 'animate']) assert.equal(target.querySelector(tag), null, tag);
    assert.equal(svg.getAttribute('class'), null); assert.equal(svg.getAttribute('pointer-events'), 'none');
    assert.equal(svg.getAttribute('onload'), null); assert.equal(svg.getAttribute('style'), null); assert.equal(svg.getAttribute('href'), null);
    assert.equal(edge.getAttribute('marker-end'), null); assert.equal(safeEdge.getAttribute('marker-end'), 'url(#local-arrow)');
    assert.equal(edge.getAttribute('d'), 'M0,0L10,10'); operation.dispose();
  });
  it('active plugin spans and code bodies stay literal without reaching Markdown processors or code scanners', async () => {
    // Static sentinels only: no script/query is evaluated by this test.
    for (const markdown of ['说明 ``  $= STATIC_SENTINEL `` 后文', '说明 `\n= STATIC_SENTINEL\n` 后文',
      '    $= STATIC_SENTINEL', '>     = STATIC_SENTINEL', '-     = STATIC_SENTINEL',
      '```text\n  $= STATIC_SENTINEL\n```', '~~~python\n= STATIC_SENTINEL\n~~~', '```mermaid\n$= STATIC_SENTINEL\n```',
      '```dataviewjs\nSTATIC_SENTINEL\n```', '```plugin-unknown\nSTATIC_SENTINEL\n```']) {
      const { target } = setup(); markdownCalls = [];
      const operation = renderKnowledgeMarkdown({}, markdown, target, '研究.md'); await operation.finished;
      assert.equal(markdownCalls.some(text => text.includes('STATIC_SENTINEL')), false, markdown);
      assert.ok(target.text.includes('STATIC_SENTINEL'), 'unsafe syntax remains readable in full');
      assert.ok(target.text.includes('仅显示原文'), 'the disabled execution boundary is explicit');
      assert.equal(target.querySelector('code'), null, 'later parent postprocessing must not find an active code node');
      operation.dispose();
    }
  });
  it('ordinary Python, JavaScript and HTML examples remain complete static code without processor languages', async () => {
    const { target } = setup();
    const examples = ['print("完整示例")', 'const answer = 42;', '<div>HTML 示例</div>'];
    const markdown = ['python','javascript','html'].map((lang,index) => '```'+lang+'\n'+examples[index]+'\n```').join('\n');
    const operation = renderKnowledgeMarkdown({}, markdown, target, '研究.md'); await operation.finished;
    assert.deepEqual(target.querySelectorAll('code').map(node => node.textContent), examples);
    assert.equal(target.querySelector('div')?.getAttribute('class') === 'language-dataviewjs', false);
    assert.equal(markdownCalls.some(text => examples.some(body => text.includes(body))), false);
    operation.dispose();
  });

  it('unsafe source and oversized/unsupported diagrams remain text and never reach the engine', async () => {
    const { target } = setup(); let calls = 0; engine.render = async () => { calls++; throw new Error('must not render'); };
    for (const source of [flow+'\nclick A "https://example.org"', '%%{init:{securityLevel:"loose"}}%%\n'+flow,
      flow+'\nclassDef danger fill:red', flow+'\nclass A malicious-plugin-class', flow+'\nA[<img src=x>]',
      flow+'\nA[&#104;ttps://example.org]', 'sequenceDiagram\nA->>B: hello', 'flowchart LR\n'+ 'A'.repeat(12001)]) {
      const operation = render(target, source); await operation.finished;
      assert.equal(target.querySelector('svg'), null); assert.ok(target.querySelector('code')); operation.dispose();
    }
    assert.equal(calls, 0);
  });
  it('a changed global security setting or engine failure is explicit and never requests Vault trust', async () => {
    const { target } = setup(); let calls = 0;
    engine.mermaidAPI.getConfig = () => ({ securityLevel: 'loose' });
    engine.render = async () => { calls++; throw new Error('parser failure'); };
    let operation = render(target); await operation.finished;
    assert.equal(calls, 0); assert.match(target.text, /不是 strict/); operation.dispose();
    engine.mermaidAPI.getConfig = () => ({ securityLevel: 'strict' });
    operation = render(target); await operation.finished;
    assert.equal(calls, 1); assert.match(target.text, /parser failure/); assert.ok(target.querySelector('code')); operation.dispose();
  });
  it('real Chromium layout keeps a long flow at readable scale and confines long prose to the knowledge drawer', async (t) => {
    const chrome = process.env.KNOWLEDGE_TEST_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    if (!existsSync(chrome)) return t.skip('installed Chromium required for physical layout, no dependency downloaded');
    const { target } = setup(); const svg = fragment.querySelector('svg');
    svg.setAttribute('viewBox', '0 0 2400 200'); svg.setAttribute('width', '100%');
    svg.querySelector('text').setAttribute('x', '20'); svg.querySelector('text').setAttribute('y', '70');
    const last = svg.createEl('text', { text: '当前结论与限制' }); last.setAttribute('x', '2230'); last.setAttribute('y', '70');
    const operation = render(target); await operation.finished;
    const escape = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    const serialize = el => `<${el.tagName.toLowerCase()} class="${escape(el.className)}" ${Object.entries(el.attributes).map(([key, value]) => `${key}="${escape(value)}"`).join(' ')}>${escape(el.textContent)}${el.children.map(serialize).join('')}</${el.tagName.toLowerCase()}>`;
    const css = readFileSync(new URL('../src/runtime/styles.css', import.meta.url), 'utf8') + '\n'
      + readFileSync(new URL('../src/console/console.css', import.meta.url), 'utf8');
    const drawerSource = readFileSync(new URL('../src/console/homepage-cosmos.js', import.meta.url), 'utf8');
    const drawerBundle = require('esbuild').buildSync({ stdin: { contents: drawerSource + '\nmodule.exports.layoutDrawer = createCosmosDrawer;', resolveDir: path.resolve('src/console'), sourcefile: 'knowledge-layout.js' }, bundle: true, platform: 'browser', format: 'iife', globalName: 'DrawerFixture', write: false }).outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
    const obsidianDir = path.join(os.homedir(), 'Library/Application Support/obsidian');
    const hostArchive = process.env.KNOWLEDGE_TEST_OBSIDIAN_ASAR || (existsSync(obsidianDir)
      ? readdirSync(obsidianDir).filter(name => /^obsidian-[\d.]+\.asar$/.test(name)).sort((a,b) => a.localeCompare(b, undefined, {numeric:true})).map(name => path.join(obsidianDir,name)).at(-1) : null);
    let nativeMermaid = '';
    if (hostArchive && existsSync(hostArchive)) {
      const archive = readFileSync(hostArchive), header = JSON.parse(archive.subarray(16,16+archive.readUInt32LE(12)).toString());
      const entry = header.files.lib.files['mermaid.min.js'], offset = 8+archive.readUInt32LE(4)+Number(entry.offset);
      nativeMermaid = archive.subarray(offset,offset+entry.size).toString().replace(/<\/script/gi, '<\\/script');
    }
    const registerSource = readFileSync(new URL('../src/console/register.js', import.meta.url),'utf8');
    const nativeRenderer = registerSource.slice(registerSource.indexOf('let knowledgeDiagramSequence = 0;'),registerSource.indexOf('// 绑定/修订类冲突'));
    const fixture = mkdtempSync(path.join(os.tmpdir(), 'knowledge-layout-'));
    try {
      const html = `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><style>body{margin:0} ${css} .markdown-rendered th{color:#111;background:#22272e} .markdown-rendered code{color:#111;background:#fff;word-break:break-all}</style>
        <div class="my-life-homepage-view"><section class="life-cosmos-home"><div class="life-cosmos-drawer-shell is-open is-knowledge-reading"><div class="life-cosmos-drawer-shade"></div><aside class="life-cosmos-drawer"><div class="life-cosmos-drawer-head"><h2>Self-RAG 完整研究</h2><button>关闭</button></div><div class="life-cosmos-drawer-body"><div class="life-knowledge-markdown life-knowledge-prose"><div class="markdown-rendered"><div><p id="long-prose">${'有证据支持的结论仍需要结合实验边界。'.repeat(12)} <code>${'very_long_source_identifier_'.repeat(18)}</code></p><table><thead><tr>${Array.from({length:8},()=>'<th>支持情况</th>').join('')}</tr></thead><tbody><tr>${Array.from({length:8},(_,i)=>'<td>验证条件 '+i+' '+('parameter'.repeat(15))+'</td>').join('')}</tr></tbody></table><table id="short-code-table"><tbody><tr>${Array.from({length:16},()=>'<td><code>lifecycle</code></td>').join('')}</tr></tbody></table></div>${serialize(target)}</div></div></div></aside></div></section></div><output id="layout-report"></output>
        <script>${nativeMermaid}</script><script>${drawerBundle}</script><script>
          const drawer=document.querySelector('.life-cosmos-drawer'),flow=document.querySelector('.life-knowledge-flow'),svg=flow.querySelector('svg'),prose=document.querySelector('#long-prose'),table=document.querySelector('table');
          const bounds=drawer.getBoundingClientRect(), fb=flow.getBoundingClientRect(), vb=svg.viewBox.baseVal;
          const range=document.createRange();range.selectNodeContents(prose);
          const metrics={viewport:innerWidth,drawerWidth:bounds.width,drawerClient:drawer.clientWidth,drawerScroll:drawer.scrollWidth,flowClient:flow.clientWidth,flowScroll:flow.scrollWidth,svgWidth:svg.getBoundingClientRect().width,effectiveFont:parseFloat(getComputedStyle(svg.querySelector('text')).fontSize)*svg.getBoundingClientRect().width/vb.width,proseWithinDrawer:[...range.getClientRects()].every(r=>r.right<=bounds.right+1),pageOverflow:document.documentElement.scrollWidth>innerWidth+1,tableContained:table.getBoundingClientRect().right<=bounds.right+1};
          const heading=table.querySelector('th'),inlineCode=prose.querySelector('code'),shortTable=document.querySelector('#short-code-table'),shortCode=shortTable.querySelector('code'),codeRange=document.createRange();codeRange.selectNodeContents(shortCode);
          metrics.markdownTheme={headingColor:getComputedStyle(heading).color,headingBackground:getComputedStyle(heading).backgroundColor,codeColor:getComputedStyle(inlineCode).color,codeBackground:getComputedStyle(inlineCode).backgroundColor,shortWordLines:codeRange.getClientRects().length,tableScroll:shortTable.scrollWidth,tableClient:shortTable.clientWidth};
          flow.scrollLeft=flow.scrollWidth;metrics.canReachEnd=flow.scrollLeft>0 && svg.querySelector('text:last-child').getBoundingClientRect().right<=fb.right+1;
          (async () => {
            const wait = () => new Promise(resolve => setTimeout(resolve, 40));
            HTMLElement.prototype.createEl = function(tag, options = {}) { const child = document.createElement(tag); if (options.cls) child.className = options.cls; if (options.text) child.textContent = options.text; this.appendChild(child); return child; };
            HTMLElement.prototype.createDiv = function(options) { return this.createEl('div', options); };
            HTMLElement.prototype.setText = function(text) { this.textContent = text; };
            HTMLElement.prototype.empty = function() { this.replaceChildren(); };
            HTMLElement.prototype.addClass = function(cls) { this.classList.add(cls); };
            HTMLElement.prototype.removeClass = function(cls) { this.classList.remove(cls); };
            if (globalThis.mermaid) {
              mermaid.initialize({startOnLoad:false,securityLevel:'strict'});
              const loadMermaid=async()=>mermaid, Component=class{load(){} unload(){}};
              const sanitizeHTMLToDom=text=>{const template=document.createElement('template');template.innerHTML=text;return template.content;};
              const MarkdownRenderer={render:async()=>{}};
              ${nativeRenderer}
              const actualHost=document.querySelector('.my-life-homepage-view').createDiv({cls:'life-knowledge-markdown'});
              const actual=renderKnowledgeMarkdown({}, ['~~~mermaid','flowchart LR','A[直接生成+效用自评] --> B[逐段评 IsRel/IsSup/IsUse]','B --> C{支持度达标?}','C --> D[输出+引用]','~~~'].join('\\n'),actualHost,'research.md');await actual.finished;
              metrics.nativeMermaidLabels=[...actualHost.querySelectorAll('svg text')].filter(text=>text.textContent.trim()).map(text=>{
                const isShape=child=>['rect','polygon','path'].includes(child.tagName.toLowerCase()) && child.getBoundingClientRect().width>1;
                let group=text.parentElement;while(group && ![...group.children].some(isShape))group=group.parentElement;
                const shape=group && [...group.children].find(isShape);
                if(!shape)return {label:text.textContent,error:'node shape missing'};
                const t=text.getBoundingClientRect(),r=shape.getBoundingClientRect();
                return {label:text.textContent,anchor:getComputedStyle(text).textAnchor,textLeft:t.left,textRight:t.right,shapeLeft:r.left,shapeRight:r.right,centerDelta:(t.left+t.right-r.left-r.right)/2};
              });
              actual.dispose();actualHost.remove();
            }
            const shared = { knowledge: null }, note = { knowledge_id: 'knowledge-'+'a'.repeat(24), revision: 2, title: '滚动恢复反例', updated_at: '2026-09-09T02:00:00Z', path: 'test.md', kind: 'research', content_status: 'qualified_conclusion', conclusion_authority: 'research_result', usable_as_current: true, freshness: { status: 'current', reasons: [] }, result: { summary: '已完成研究', answer_markdown: '异步长回答', unknowns: [], confirmed: [] }, evidence: [] };
            const options = { viewState: shared, sources: { readKnowledge: async () => { await wait(); return note; } }, capabilities: { 'knowledge.renderMarkdown': (text, target) => ({ dispose() {}, finished: wait().then(() => { target.empty(); for (let i=0;i<80;i++) target.createEl('p', {text: '正文第 '+i+' 段：真实 DOM 中的连续阅读位置。'}); const graph=target.createDiv({cls:'life-knowledge-flow'}); graph.tabIndex=0; graph.appendChild(svg.cloneNode(true)); }) }) } };
            document.querySelector('.my-life-homepage-view').remove();
            const outer = document.body.createDiv({ cls: 'native-preview-scroller' }); outer.style.cssText='height:100vh;overflow:auto;';
            const root = outer.createDiv({ cls: 'my-life-homepage-view' }); root.style.cssText='height:2851px;transform:translateY(40px) scale(1.36);transform-origin:top left;'; root.style.zoom=new URLSearchParams(location.search).get('zoom')||'1';
            outer.scrollTop=1200;
            let host = root.createDiv({ cls: 'life-cosmos-home' }), instance = DrawerFixture.layoutDrawer(document, host, options);
            await instance.showKnowledge(note); await wait(); await wait();
            let pane = host.querySelector('.life-cosmos-drawer');
            metrics.answerInitiallyClosed=!pane.querySelector('.life-knowledge-answer').open;
            metrics.noHiddenDiagram=pane.querySelector('.life-knowledge-flow')===null;
            pane.querySelector('.life-knowledge-answer').open=true; await wait(); await wait();
            let nativeShell=host.querySelector('.life-cosmos-drawer-shell');
            metrics.viewportBoundBefore={left:nativeShell.getBoundingClientRect().left,right:nativeShell.getBoundingClientRect().right,width:nativeShell.getBoundingClientRect().width,viewportWidth:innerWidth,top:nativeShell.getBoundingClientRect().top,height:nativeShell.getBoundingClientRect().height,viewportHeight:innerHeight,modal:nativeShell.matches(':modal')};
            pane.scrollTop = pane.scrollHeight; await wait();
            const readingGraph=pane.querySelector('.life-knowledge-flow'); readingGraph.scrollLeft=600; readingGraph.focus({preventScroll:true}); await wait();
            metrics.knowledgeDrawerBounds={top:pane.getBoundingClientRect().top,bottom:pane.getBoundingClientRect().bottom,viewportHeight:innerHeight,left:pane.getBoundingClientRect().left,right:pane.getBoundingClientRect().right,width:pane.getBoundingClientRect().width}; metrics.readingBefore = pane.scrollTop;
            instance.projectionChanged(); await wait(); metrics.readingAfterProjection = pane.scrollTop;
            outer.scrollTop=0; await wait(); metrics.viewportAfterOuterReset={top:nativeShell.getBoundingClientRect().top,height:nativeShell.getBoundingClientRect().height,reading:pane.scrollTop};
            host.remove(); metrics.readingDetached = pane.scrollTop; instance.dispose();
            metrics.readingSaved = shared.knowledge.scrollTop; metrics.answerExpansionSaved=shared.knowledge.answerExpanded;
            host = root.createDiv({ cls: 'life-cosmos-home' }); instance = DrawerFixture.layoutDrawer(document, host, options);
            await instance.restoreKnowledge(); await wait(); pane = host.querySelector('.life-cosmos-drawer');
            nativeShell=host.querySelector('.life-cosmos-drawer-shell');
            metrics.viewportBoundRestored={left:nativeShell.getBoundingClientRect().left,right:nativeShell.getBoundingClientRect().right,width:nativeShell.getBoundingClientRect().width,viewportWidth:innerWidth,top:nativeShell.getBoundingClientRect().top,height:nativeShell.getBoundingClientRect().height,viewportHeight:innerHeight,modal:nativeShell.matches(':modal')};
            metrics.answerExpansionRestored=pane.querySelector('.life-knowledge-answer').open;
            metrics.readingRestored = pane.scrollTop; metrics.diagramRestored=pane.querySelector('.life-knowledge-flow').scrollLeft; metrics.diagramFocused=document.activeElement===pane.querySelector('.life-knowledge-flow'); metrics.readingScrollOwner = { drawer: pane.scrollHeight-pane.clientHeight, shell: host.querySelector('.life-cosmos-drawer-shell').scrollHeight-host.querySelector('.life-cosmos-drawer-shell').clientHeight };
            metrics.nativeClosePaths=[];
            instance.dispose();
            metrics.disposedModal=document.querySelector(':modal')!==null;
            const trigger=host.createEl('button',{text:'知识入口'});
            instance=DrawerFixture.layoutDrawer(document,host,options);
            for(const method of ['cancel','shade','button']) {
              await instance.showKnowledge(note,trigger); await wait(); await wait();
              const dialog=host.querySelector('dialog.is-open');
              const wasModal=dialog.matches(':modal');
              if(method==='cancel') dialog.dispatchEvent(new Event('cancel',{cancelable:true}));
              if(method==='shade') dialog.querySelector('.life-cosmos-drawer-shade').click();
              if(method==='button') dialog.querySelector('.life-cosmos-drawer-close').click();
              await wait();
              metrics.nativeClosePaths.push({method,wasModal,closed:!dialog.open,focusRestored:document.activeElement===trigger,inertRestored:!trigger.inert,selectionCleared:shared.knowledge===null});
            }
            instance.showSettings(trigger, {}); await wait();
            const actionPane=host.querySelector('dialog.is-open .life-cosmos-drawer');
            metrics.actionDrawerBounds={top:actionPane.getBoundingClientRect().top,bottom:actionPane.getBoundingClientRect().bottom,viewportHeight:innerHeight,left:actionPane.getBoundingClientRect().left,right:actionPane.getBoundingClientRect().right,width:actionPane.getBoundingClientRect().width};
            await instance.showIntent({ fragment_id:'raw-layout', source_origin:'raw_capture', title:'MarkupSafe 是否适合安全显示网页摘录？', status:'passed', route:'verify', execution:{ run_id:'exec:raw-layout', status:'passed', route:'verify', result_digest:'b'.repeat(64), research_progress:{stage:'synthesized',cognitive:'synthesized'}, result:{summary:'可以用于 HTML 文本转义，但不能直接把不可信输入标为 Markup。'.repeat(12), recommendation:'使用 escape 并按输出上下文处理安全边界。',unknowns:[],answer_markdown:'完整代码与限制'} } },null,trigger);
            await wait();await wait();
            const fragmentPane=host.querySelector('dialog.is-open .life-cosmos-drawer'),card=fragmentPane.querySelector('.life-cosmos-intent-row');
            card.querySelector('.life-knowledge-answer').open=true; await wait(); await wait();
            // Static code is deliberately wider than the drawer; only its pre may scroll.
            const codeHost=card.querySelector('.life-knowledge-markdown')||card;
            const pre=codeHost.createEl('pre');pre.createEl('code',{text:'escape(untrusted_source_identifier_'+('long_'.repeat(45))+')'});
            const summary=card.querySelector('.life-knowledge-prose'),textRange=document.createRange();textRange.selectNodeContents(summary);
            const cardRect=card.getBoundingClientRect(),fragmentRect=fragmentPane.getBoundingClientRect();
            metrics.fragmentBounds={drawerWidth:fragmentRect.width,drawerLeft:fragmentRect.left,cardRight:cardRect.right,drawerRight:fragmentRect.right,client:fragmentPane.clientWidth,scroll:fragmentPane.scrollWidth,summaryWithin:[...textRange.getClientRects()].every(r=>r.right<=fragmentRect.right+1),preScroll:pre.scrollWidth,preClient:pre.clientWidth};
            instance.dispose();
            const modal=document.body.createDiv({cls:'modal my-life-quick-capture-modal'}); modal.style.padding='16px'; modal.style.boxSizing='border-box';
            const setting=modal.createDiv({cls:'setting-item'}), control=setting.createDiv({cls:'setting-item-control'}), area=control.createEl('textarea'); area.value='原始问题内容'.repeat(100);
            metrics.captureWidth=modal.getBoundingClientRect().width; metrics.captureInputHeight=area.getBoundingClientRect().height; metrics.captureWithinModal=area.getBoundingClientRect().right<=modal.getBoundingClientRect().right;
            document.querySelector('#layout-report').textContent=JSON.stringify(metrics);
          })().catch(error => { document.querySelector('#layout-report').textContent=JSON.stringify({error:String(error), stack:error.stack}); });
        </script>`;
      const file = path.join(fixture, 'layout.html'); writeFileSync(file, html);
      const results = [];
      for (const [width,zoom] of [[1224,1],[520,1],[1224,1.14],[520,1.14],[1224,1.36],[520,1.36]]) {
        const output = await new Promise((resolve, reject) => {
          const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions', '--disable-default-apps', '--no-first-run', '--no-default-browser-check', '--force-device-scale-factor=1', '--virtual-time-budget=2500', `--user-data-dir=${path.join(fixture, 'profile-'+width+'-'+zoom)}`, `--window-size=${width},900`, '--dump-dom', 'file://'+file+'?zoom='+zoom], { detached: true, stdio: ['ignore', 'pipe', 'ignore'] });
          let output = ''; let finished = false;
          const finish = (error) => {
            if (finished) return; finished = true; clearTimeout(timer);
            // Only this fixture's isolated Chrome process group is stopped.
            try { process.kill(-child.pid, 'SIGTERM'); } catch {}
            if (error) reject(error); else resolve(output);
          };
          const timer = setTimeout(() => finish(new Error('Chromium did not return layout measurements')), 20000);
          child.stdout.setEncoding('utf8');
          child.stdout.on('data', chunk => { output += chunk; if (/<output id="layout-report">\{.*?\}<\/output>/s.test(output)) finish(); });
          child.on('error', finish);
          child.on('exit', code => { if (!finished) finish(new Error('Chromium exited before layout measurements: '+code)); });
        });
        const result = JSON.parse(output.match(/<output id="layout-report">(.*?)<\/output>/s)?.[1] || '{}'); results.push(result);
        t.diagnostic(JSON.stringify(result));
        for (const bounds of [result.viewportBoundBefore,result.viewportBoundRestored]) {
          assert.ok(Math.abs(bounds.left)<1 && Math.abs(bounds.right-bounds.viewportWidth)<1, 'both horizontal viewport edges must stay visible: '+JSON.stringify(result));
          assert.equal(bounds.modal,true, 'the native dialog must escape transformed preview ancestors');
          assert.ok(Math.abs(bounds.top)<1 && Math.abs(bounds.height-bounds.viewportHeight)<1, JSON.stringify(result));
        }
        assert.ok(Math.abs(result.viewportAfterOuterReset.top)<1, 'resetting the real preview scroll must not move the reading dialog');
        assert.equal(result.viewportAfterOuterReset.reading,result.readingAfterProjection);
        for (const bounds of [result.knowledgeDrawerBounds,result.actionDrawerBounds]) assert.ok(bounds.top>=-1 && bounds.bottom<=bounds.viewportHeight+1 && bounds.left>=-1 && bounds.right<=result.viewport+1 && bounds.width>=300, 'knowledge and action drawers must both fit the viewport: '+JSON.stringify(result));
        assert.ok(result.fragmentBounds.cardRight<=result.fragmentBounds.drawerRight+1 && result.fragmentBounds.summaryWithin && result.fragmentBounds.scroll<=result.fragmentBounds.client+1, 'raw research card and its summary must stay inside the action drawer: '+JSON.stringify(result.fragmentBounds));
        assert.ok(result.fragmentBounds.preScroll>result.fragmentBounds.preClient, 'long source code scrolls independently instead of expanding the card');
        if(width===1224 && zoom===1) {
          assert.ok(result.fragmentBounds.drawerWidth>=820 && result.fragmentBounds.drawerWidth<=920,'research details need real reading width');
          assert.equal(result.actionDrawerBounds.width,520,'system-management drawer keeps its original operation width');
        }
        assert.ok(result.fragmentBounds.drawerLeft>=-1 && result.fragmentBounds.drawerRight<=width+1,'research reading drawer must fit narrow and scaled viewports');
        assert.equal(result.answerInitiallyClosed,true,'full answer starts collapsed');
        assert.equal(result.noHiddenDiagram,true,'hidden answer must not measure or render a diagram');
        assert.equal(result.answerExpansionSaved,true,'reading snapshot records the expanded answer');
        assert.equal(result.answerExpansionRestored,true,'remount reopens the same reading section before restoring scroll and diagram focus');
        assert.equal(result.markdownTheme.headingColor,'rgb(240, 243, 246)');
        assert.equal(result.markdownTheme.codeColor,'rgb(240, 243, 246)');
        assert.equal(result.markdownTheme.codeBackground,'rgb(24, 27, 32)');
        assert.equal(result.markdownTheme.shortWordLines,1,'short inline code words must not break into single letters');
        assert.ok(result.markdownTheme.tableScroll>result.markdownTheme.tableClient,'wide table scrolls instead of shrinking each code word');
        if (nativeMermaid) {
          assert.equal(result.nativeMermaidLabels.length,4, 'same installed Mermaid must produce all four node labels');
          for(const label of result.nativeMermaidLabels) assert.ok(Math.abs(label.centerDelta)<2 && label.textLeft>=label.shapeLeft-1 && label.textRight<=label.shapeRight+1,'sanitized real Mermaid labels must remain centered inside nodes: '+JSON.stringify(label));
        }
        assert.equal(result.disposedModal,false, 'dispose must leave no top-layer modal');
        for(const close of result.nativeClosePaths) assert.ok(close.wasModal && close.closed && close.focusRestored && close.inertRestored && close.selectionCleared, JSON.stringify(close));
        assert.equal(result.diagramRestored,600, 'same revision retains horizontal diagram reading');
        assert.equal(result.diagramFocused,true, 'remount retains the focused diagram instead of trapping reading on Close');
        assert.ok(result.captureWidth >= (width>620?700:450) && result.captureInputHeight >= 240 && result.captureWithinModal, JSON.stringify(result));
        assert.ok(result.readingBefore > 500, JSON.stringify(result));
        assert.ok(Math.abs(result.readingRestored - result.readingAfterProjection) < 2, 'detached DOM and delayed Markdown must preserve the last reading position: '+JSON.stringify(result));
        assert.ok(result.drawerWidth >= (width > 620 ? 800 : width - 2), JSON.stringify(result));
        assert.ok(result.effectiveFont >= 14, 'diagram labels must remain readable: '+JSON.stringify(result));
        assert.ok(result.flowScroll > result.flowClient && result.canReachEnd, 'the wide diagram must scroll to its final node');
        assert.ok(result.proseWithinDrawer && result.tableContained && !result.pageOverflow, JSON.stringify(result));
        assert.ok(result.drawerScroll <= result.drawerClient + 1, 'horizontal overflow belongs to the diagram/table, not the reading drawer');
      }
      t.diagnostic(JSON.stringify(results));
    } finally { operation.dispose(); rmSync(fixture, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
  });

  it('closing during native rendering removes temporary measurement and ignores late SVG', async () => {
    const { doc, target } = setup(); let resolve;
    engine.render = () => new Promise(done => { resolve = done; });
    const operation = render(target);
    for (let i = 0; i < 5; i++) await Promise.resolve();
    assert.ok(doc.querySelector('.life-knowledge-flow-measure'));
    operation.dispose(); resolve({ svg: '<native-svg-fixture>' }); await operation.finished;
    assert.equal(doc.querySelector('.life-knowledge-flow-measure'), null); assert.equal(target.querySelector('svg'), null);
    assert.equal(components[0].unloads, 1);
  });
});
