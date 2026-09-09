import { it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); };
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }

function harness(options = {}) {
  const calls = [], commands = [], disposers = [], notices = [], order = [], layoutChanges = [];
  let electronLoads = 0;
  const webFrame = {
    setVisualZoomLevelLimits(min, max) {
      calls.push([min, max]); order.push('visual');
      return options.nativeCall ? options.nativeCall(min, max, calls.length) : Promise.resolve();
    },
    setZoomFactor(...args) { layoutChanges.push(['factor', ...args]); },
    setZoomLevel(...args) { layoutChanges.push(['level', ...args]); },
  };
  class BaselinePlugin {
    async onload() { order.push('baseline'); }
    registerView() {}
    register(callback) { disposers.push(callback); }
    addCommand(command) { commands.push(command); }
  }
  const context = {
    module: { exports: {} }, console,
    require(name) {
      if (name === './standalone-view.js') return { CosmosView: class {}, COSMOS_VIEW_TYPE: 'loop-graph-cosmos' };
      if (name === './runtime/main.js') return BaselinePlugin;
      if (name === './console/register.js') return { setupLoopConsole() { order.push('console'); } };
      if (name === './console/thought-map.js') return { collectThoughtMap() {} };
      if (name === './console/quick-capture-modal.js') return { LargeQuickCaptureModal: class {} };
      if (name === 'obsidian') return { Platform: { isDesktopApp: options.desktop !== false }, Notice: class { constructor(text) { notices.push(text); } } };
      throw new Error('Unexpected main dependency: ' + name);
    },
    window: { require(name) {
      assert.equal(name, 'electron'); electronLoads += 1;
      if (options.requireError) throw new Error('Electron unavailable');
      return Object.hasOwn(options, 'electron') ? options.electron : { webFrame };
    } },
  };
  if (options.noWindow) delete context.window;
  if (options.noRequire) context.window.require = undefined;
  runInNewContext(source, context, { filename: 'src/main.js' });
  const plugin = new context.module.exports();
  const unload = async () => { for (const dispose of disposers) dispose(); await settle(); };
  return { plugin, calls, commands, notices, order, layoutChanges, unload, electronLoads: () => electronLoads };
}

it('desktop enables native visual pinch after normal startup and reset preserves layout zoom', async () => {
  const h = harness();
  await h.plugin.onload();
  assert.deepEqual(h.order.slice(0, 3), ['baseline', 'console', 'visual']);
  assert.deepEqual(h.calls, [[1, 3]]);
  const reset = h.commands.find(command => command.id === 'reset-pinch-zoom');
  assert.ok(reset, 'reset is an actual plugin command');
  await reset.callback();
  assert.deepEqual(h.calls, [[1, 3], [1, 1], [1, 3]]);
  await h.unload();
  assert.deepEqual(h.calls.at(-1), [1, 1]);
  assert.deepEqual(h.layoutChanges, [], 'visual pinch must not change Cmd zoom/layout scaling');
});

it('mobile never loads Electron or registers a visual zoom command', async () => {
  const h = harness({ desktop: false });
  await h.plugin.onload(); await h.unload();
  assert.deepEqual(h.order, ['baseline', 'console']);
  assert.equal(h.electronLoads(), 0); assert.deepEqual(h.calls, []); assert.deepEqual(h.commands, []);
});

for (const options of [{ noWindow: true }, { noRequire: true }, { requireError: true }, { electron: {} }, { electron: { webFrame: {} } }]) {
  it(`unavailable native capability does not prevent startup: ${JSON.stringify(options)}`, async () => {
    const h = harness(options);
    await assert.doesNotReject(h.plugin.onload()); await h.unload();
    assert.deepEqual(h.order.slice(0, 2), ['baseline', 'console']);
    assert.deepEqual(h.commands, []); assert.deepEqual(h.layoutChanges, []);
  });
}

for (const asynchronous of [false, true]) {
  it(`native initialization ${asynchronous ? 'rejection' : 'throw'} is contained`, async () => {
    const h = harness({ nativeCall() { if (asynchronous) return Promise.reject(new Error('native failure')); throw new Error('native failure'); } });
    await assert.doesNotReject(h.plugin.onload()); await h.unload();
    assert.ok(h.notices.length > 0); assert.deepEqual(h.commands, []); assert.deepEqual(h.layoutChanges, []);
  });
}

it('unload contains native cleanup rejection', async () => {
  const h = harness({ nativeCall(min, max, n) { return n === 1 ? Promise.resolve() : Promise.reject(new Error('cleanup unavailable')); } });
  await h.plugin.onload(); await assert.doesNotReject(h.unload());
  assert.deepEqual(h.calls, [[1, 3], [1, 1]]);
});

it('reset reports native failure without changing layout zoom', async () => {
  const h = harness({ nativeCall(min, max, n) { return n === 2 ? Promise.reject(new Error('reset unavailable')) : Promise.resolve(); } });
  await h.plugin.onload();
  await assert.doesNotReject(h.commands.find(command => command.id === 'reset-pinch-zoom').callback());
  assert.ok(h.notices.length > 0); assert.deepEqual(h.layoutChanges, []);
  await h.unload();
});

it('late initialization after unload cannot register the reset command', async () => {
  const gate = deferred(), h = harness({ nativeCall(min, max, n) { return n === 1 ? gate.promise : Promise.resolve(); } });
  const loading = h.plugin.onload(); await settle();
  await h.unload(); gate.resolve(); await loading;
  assert.deepEqual(h.calls, [[1, 3], [1, 1]]); assert.deepEqual(h.commands, []);
});

it('reset waits for the 100% clamp and cannot reopen pinch after unload', async () => {
  const gate = deferred(), h = harness({ nativeCall(min, max, n) { return n === 2 ? gate.promise : Promise.resolve(); } });
  await h.plugin.onload();
  const resetting = h.commands.find(command => command.id === 'reset-pinch-zoom').callback();
  await settle(); assert.deepEqual(h.calls, [[1, 3], [1, 1]], 're-enable must wait for clamp completion');
  await h.unload(); gate.resolve(); await resetting;
  assert.deepEqual(h.calls, [[1, 3], [1, 1], [1, 1]]); assert.deepEqual(h.layoutChanges, []);
});
