import { it } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';
const require = createRequire(import.meta.url);
const Module = require('node:module');
const load = Module._load;
Module._load = function(name, ...rest) {
  if (name === 'obsidian') return { ItemView: class { constructor() { this.contentEl = new FakeEl('div'); } } };
  return load.call(this, name, ...rest);
};
const { CosmosView } = require('../src/standalone-view.js');
const { prepareHomepageCosmos } = require('../src/console/homepage-cosmos.js');
Module._load = load;
it('fresh vault workbench mounts without a homepage note and disposes on close', async () => {
  let calls = 0, disposed = 0;
  const doc = { createElement: tag => new FakeEl(tag) };
  const plugin = { async mountHomepageCosmos(root) {
    const session = prepareHomepageCosmos(doc, root);
    assert.equal(session.home, view.contentEl);
    assert.ok(session.dashboard);
    session.mount.__lifeCosmosDispose = () => { disposed++; };
    calls++;
  } };
  const view = new CosmosView({}, plugin);
  await view.onOpen();
  assert.equal(calls, 1);
  await view.onClose();
  assert.equal(disposed, 1);
  assert.equal(view.contentEl.children.length, 0);
});
