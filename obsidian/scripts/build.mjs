import { copyFile, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC = path.join(ROOT, 'src', 'runtime');
const BASE = path.join(ROOT, 'baseline', 'runtime');
const DIST = path.join(ROOT, 'dist');
const ENTRY = path.join(ROOT, 'src', 'main.js');
const CONSOLE_CSS = path.join(ROOT, 'src', 'console', 'console.css');
const FILES = ['main.js', 'styles.css', 'manifest.json'];

const CSS_MARKER_BEGIN = '\n/* ==== P1 LOOP CONSOLE STYLES BEGIN (appended deterministically by scripts/build.mjs) ==== */\n';
const CSS_MARKER_END = '\n/* ==== P1 LOOP CONSOLE STYLES END ==== */\n';

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

async function assertByteIdenticalToBaseline() {
  for (const name of FILES) {
    const srcBuf = await readFile(path.join(SRC, name));
    const baseBuf = await readFile(path.join(BASE, name));
    if (!srcBuf.equals(baseBuf)) {
      throw new Error(
        `P0 gate: src/runtime/${name} differs from baseline/runtime/${name}\n` +
        `  src:      ${sha256(srcBuf)}\n` +
        `  baseline: ${sha256(baseBuf)}\n` +
        'Refusing to build. Restore src/runtime from baseline before building.'
      );
    }
  }
}

async function pruneDist() {
  await mkdir(DIST, { recursive: true });
  const allowed = new Set(FILES);
  for (const entry of await readdir(DIST)) {
    if (!allowed.has(entry)) {
      await rm(path.join(DIST, entry), { recursive: true, force: true });
      console.log(`removed unexpected dist entry: ${entry}`);
    }
  }
}

async function bundleMain() {
  const { buildSync } = await import('esbuild');
  // Baseline-derived outputs may be read-only. Remove the previous bundle
  // before esbuild opens the output path, matching the manifest/CSS flow.
  await rm(path.join(DIST, 'main.js'), { force: true });
  // Frozen packaging configuration (TASK-P1.md "Build-gate evolution", item 4).
  const result = buildSync({
    entryPoints: [ENTRY],
    bundle: true,
    format: 'cjs',
    platform: 'node',
    external: ['obsidian'],
    charset: 'utf8',
    minify: false,
    sourcemap: false,
    outfile: path.join(DIST, 'main.js'),
    logLevel: 'silent',
  });
  if (result.errors.length > 0) {
    throw new Error(`esbuild failed: ${result.errors.map((e) => e.text).join('; ')}`);
  }
}

async function composeStyles() {
  const baselineCss = await readFile(path.join(BASE, 'styles.css'));
  const consoleCss = await readFile(CONSOLE_CSS);
  const composed = Buffer.concat([
    baselineCss,
    Buffer.from(CSS_MARKER_BEGIN, 'utf8'),
    consoleCss,
    Buffer.from(CSS_MARKER_END, 'utf8'),
  ]);
  const outPath = path.join(DIST, 'styles.css');
  await rm(outPath, { force: true });
  await writeFile(outPath, composed);
}

async function copyManifest() {
  const outPath = path.join(DIST, 'manifest.json');
  await rm(outPath, { force: true });
  await copyFile(path.join(BASE, 'manifest.json'), outPath);
}

async function main() {
  console.log('P0 gate: verifying src/runtime is byte-identical to baseline/runtime ...');
  await assertByteIdenticalToBaseline();
  console.log('  ok: all three baseline runtime files match baseline');

  await pruneDist();

  await bundleMain();
  await composeStyles();
  await copyManifest();

  for (const name of FILES) {
    const outPath = path.join(DIST, name);
    const outBuf = await readFile(outPath);
    const info = await stat(outPath);
    console.log(`dist/${name}  ${sha256(outBuf)}  (${info.size} bytes)`);
  }

  const manifest = JSON.parse(await readFile(path.join(DIST, 'manifest.json'), 'utf8'));
  if (manifest.id !== 'my-life-homepage') {
    throw new Error('dist/manifest.json lost plugin id my-life-homepage');
  }
  const baseManifest = await readFile(path.join(BASE, 'manifest.json'));
  if (!(await readFile(path.join(DIST, 'manifest.json'))).equals(baseManifest)) {
    throw new Error('dist/manifest.json must stay byte-identical to baseline');
  }

  const entries = await readdir(DIST);
  if (entries.length !== FILES.length) {
    throw new Error(`dist must contain exactly ${FILES.length} files, found ${entries.length}`);
  }
  console.log('build ok: dist contains exactly main.js, styles.css, manifest.json');
}

const invokedAs = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedAs) {
  main().then(() => process.exit(0)).catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}
