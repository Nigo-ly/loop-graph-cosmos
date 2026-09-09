import { readFile, readdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC = path.join(ROOT, 'src', 'runtime');
const BASE = path.join(ROOT, 'baseline', 'runtime');
const DIST = path.join(ROOT, 'dist');
const CONSOLE_SRC = path.join(ROOT, 'src', 'console');
const FIXTURES = path.join(ROOT, 'fixtures', 'p1', 'synthetic');
const FILES = ['main.js', 'styles.css', 'manifest.json'];
const SCRIPTS = ['build.mjs', 'check.mjs', 'deploy.mjs', 'rollback.mjs', 'production-release.mjs', 'install-cognitive-decision-launchagent.sh', 'cognitive-decision-status.sh', 'rollback-cognitive-decision-launchagent.sh'];
const CONSOLE_MODULES = ['api-client.js', 'auth-queue-cards.js', 'control-client.js', 'cognitive-decision-client.js', 'product-review-client.js', 'product-review-dashboard.js', 'fragment-continuation-client.js', 'fragment-intent-client.js', 'fragment-intent-card.js', 'proposal-client.js', 'review-client.js', 'view-model.js', 'console-dom.js', 'console-view.js', 'thought-map.js', 'home-entry.js', 'homepage-cosmos.js', 'register.js', 'graph-client.js', 'graph-view-model.js', 'graph-view.js', 'graph-home-card.js', 'graph-canvas-model.js', 'graph-canvas-layout.js', 'graph-canvas-view.js', 'graph-pilot-bridge.js'];
const CONTROL_CLIENT = 'control-client.js';
const COGNITIVE_DECISION_CLIENT = 'cognitive-decision-client.js';
const PRODUCT_REVIEW_CLIENT = 'product-review-client.js';
const CONTINUATION_CLIENT = 'fragment-continuation-client.js';
const FRAGMENT_INTENT_CLIENT = 'fragment-intent-client.js';
const REVIEW_CLIENT = 'review-client.js';
const GRAPH_CLIENT = 'graph-client.js';
const PINNED_ESBUILD = '0.28.1';

const CSS_MARKER_BEGIN = '/* ==== P1 LOOP CONSOLE STYLES BEGIN (appended deterministically by scripts/build.mjs) ==== */';
const CSS_MARKER_END = '/* ==== P1 LOOP CONSOLE STYLES END ==== */';

const results = [];
function record(name, ok, detail = '') {
  results.push({ name, ok, detail });
}

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

async function checkPackageJson() {
  try {
    const pkg = JSON.parse(await readFile(path.join(ROOT, 'package.json'), 'utf8'));
    record('package.json parses', true);
    record('package name', pkg.name === 'my-life-loop-control-ui', pkg.name);
    record('package is private', pkg.private === true);
    record('packageManager pinned', typeof pkg.packageManager === 'string' && /^npm@\d+\.\d+\.\d+$/.test(pkg.packageManager), pkg.packageManager);
    record('engines.node pinned', typeof pkg.engines?.node === 'string' && /^\d+\.\d+\.\d+$/.test(pkg.engines.node), pkg.engines?.node);
    const deps = Object.keys(pkg.dependencies ?? {}).length;
    record('zero runtime dependencies', deps === 0, `${deps} deps`);
    const devDeps = pkg.devDependencies ?? {};
    const devNames = Object.keys(devDeps);
    record(
      'devDependencies limited to pinned esbuild',
      devNames.length === 1 && devNames[0] === 'esbuild' && devDeps.esbuild === PINNED_ESBUILD,
      JSON.stringify(devDeps),
    );
    for (const s of ['check', 'test', 'build', 'deploy:dry-run', 'rollback:dry-run', 'deploy:apply', 'rollback:apply', 'control:install', 'control:status', 'control:rollback', 'cognitive:install', 'cognitive:status', 'cognitive:rollback']) {
      record(`script "${s}" defined`, typeof pkg.scripts?.[s] === 'string', pkg.scripts?.[s]);
    }
  } catch (err) {
    record('package.json parses', false, err.message);
  }
  try {
    const lock = JSON.parse(await readFile(path.join(ROOT, 'package-lock.json'), 'utf8'));
    const entry = lock.packages?.['node_modules/esbuild'];
    record('lockfile pins esbuild', entry?.version === PINNED_ESBUILD && typeof entry?.integrity === 'string', entry?.version);
  } catch (err) {
    record('lockfile pins esbuild', false, err.message);
  }
}

async function checkManifest() {
  try {
    const raw = await readFile(path.join(BASE, 'manifest.json'), 'utf8');
    const manifest = JSON.parse(raw);
    record('baseline manifest parses', true);
    record('plugin id is my-life-homepage', manifest.id === 'my-life-homepage', manifest.id);
    record('manifest has name/version/minAppVersion',
      typeof manifest.name === 'string' && typeof manifest.version === 'string' && typeof manifest.minAppVersion === 'string',
      `${manifest.name}@${manifest.version}`);
  } catch (err) {
    record('baseline manifest parses', false, err.message);
  }
}

async function checkSyntax() {
  for (const name of SCRIPTS) {
    const file = path.join(ROOT, 'scripts', name);
    if (!existsSync(file)) {
      record(`syntax: scripts/${name}`, false, 'file missing');
      continue;
    }
    try {
      if (name.endsWith('.sh')) {
        record(`syntax: scripts/${name}`, true, 'checked by sh -n in tests');
        continue;
      }
      await import(pathToFileURL(file).href);
      record(`syntax: scripts/${name}`, true);
    } catch (err) {
      record(`syntax: scripts/${name}`, false, err.message);
    }
  }
  try {
    const code = await readFile(path.join(SRC, 'main.js'), 'utf8');
    new vm.Script(code, { filename: 'src/runtime/main.js' });
    record('syntax: src/runtime/main.js', true);
  } catch (err) {
    record('syntax: src/runtime/main.js', false, err.message);
  }
  try {
    const code = await readFile(path.join(ROOT, 'src', 'main.js'), 'utf8');
    new vm.Script(code, { filename: 'src/main.js' });
    record('syntax: src/main.js', true);
  } catch (err) {
    record('syntax: src/main.js', false, err.message);
  }
  for (const name of CONSOLE_MODULES) {
    const file = path.join(CONSOLE_SRC, name);
    if (!existsSync(file)) {
      record(`syntax: src/console/${name}`, false, 'file missing');
      continue;
    }
    try {
      const code = await readFile(file, 'utf8');
      new vm.Script(code, { filename: `src/console/${name}` });
      record(`syntax: src/console/${name}`, true);
    } catch (err) {
      record(`syntax: src/console/${name}`, false, err.message);
    }
  }
}

const FORBIDDEN_PATTERNS = [
  { name: 'SQLite reference', re: /sqlite/i },
  { name: 'frontmatter mutation', re: /processFrontMatter/ },
  { name: 'vault mutation', re: /vault\.(process|modify|create|append|delete|createFolder)\b/ },
  { name: 'non-GET HTTP method', re: /method:\s*["'`](POST|PUT|DELETE|PATCH|HEAD|OPTIONS)["'`]/i },
  { name: 'fetch() usage', re: /\bfetch\s*\(/ },
  { name: 'n8n webhook reference', re: /127\.0\.0\.1:5678|fragment-organize-now/ },
];

// R1-OC revision 3: the cognitive client may POST exactly two frozen paths
// under the fixed loopback 5684 base — /decisions and /withdrawals, one call
// site each — and each site must carry its own frozen header
// (X-Fragment-Cognitive-Decision / X-Fragment-Cognitive-Withdrawal). Any
// other POST, any foreign URL, or a baseUrl override fails the guard.
// Exported so the R1-OC test suite can pin this behaviour directly.
export function cognitiveClientGuardHits(code) {
  const hits = FORBIDDEN_PATTERNS.filter(
    (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
  ).map((p) => p.name);
  const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
  const foreignUrls = urls.filter(
    (url) => !url.startsWith('http://127.0.0.1:5684/fragment-cognitive/v1'),
  );
  if (foreignUrls.length) hits.push(`foreign URL: ${foreignUrls.join(', ')}`);
  if (/options\.baseUrl/.test(code)) hits.push('baseUrl override still present');
  // Exactly two POST call sites, however they are quoted — no more, no less.
  const postSites = (code.match(/method:\s*["'`]POST["'`]/g) || []).length;
  if (postSites !== 2) hits.push(`POST sites: ${postSites}`);
  // Exactly the two frozen paths, each exactly once, built from the fixed base.
  const postPaths = [
    ...code.matchAll(/url:\s*`\$\{COGNITIVE_DECISION_API_BASE_URL\}(\/[^`\s]+)`/g),
  ].map((m) => m[1]);
  const sortedPaths = [...postPaths].sort();
  if (sortedPaths.length !== 2 || sortedPaths[0] !== '/decisions' || sortedPaths[1] !== '/withdrawals') {
    hits.push(`write paths: ${JSON.stringify(postPaths)}`);
  }
  // Each POST site must carry its own frozen header; the decision header must
  // never masquerade a withdrawal.
  const headerBlock = (frozenPath) => {
    const match = code.match(
      new RegExp(
        `url:\\s*\`\\$\\{COGNITIVE_DECISION_API_BASE_URL\\}${frozenPath}\`,[\\s\\S]*?headers:\\s*\\{([\\s\\S]*?)\\}`,
      ),
    );
    return match ? match[1] : null;
  };
  const decisionHeaders = headerBlock('/decisions');
  if (!decisionHeaders || !decisionHeaders.includes('[DECISION_HEADER]: "1"')) {
    hits.push('decision POST missing X-Fragment-Cognitive-Decision header');
  }
  const withdrawalHeaders = headerBlock('/withdrawals');
  if (!withdrawalHeaders || !withdrawalHeaders.includes('[WITHDRAWAL_HEADER]: "1"')) {
    hits.push('withdrawal POST missing X-Fragment-Cognitive-Withdrawal header');
  }
  if (withdrawalHeaders && withdrawalHeaders.includes('[DECISION_HEADER]')) {
    hits.push('withdrawal POST reuses the decision header');
  }
  return hits;
}

// R26 P2-2 / R27 P1-2 / R28 P1-1（gate_2dadfb90d539）：register.js 已批准
// sidecar 写守卫——三处 writeFileSync/mkdirSync 各自绑定获批目标形状；拒绝集
// 覆盖常规 fs 写入口（含 fs.writeFile / fs.promises.writeFile 旁路）。
// 导出供 checkWriteGuardSelfTests 每次门禁实跑内存变异。
export function registerWriteGuardHits(code) {
  const hits = [];
  const writeSites = (code.match(/fs\.writeFileSync/g) || []).length;
  const mkdirTotal = (code.match(/fs\.mkdirSync/g) || []).length;
  const mkdirBound = (code.match(/fs\.mkdirSync\(dir, \{ recursive: true \}\)/g) || []).length;
  const dirRefs = (code.match(/loop-graph-cosmos/g) || []).length;
  if (writeSites !== 3) hits.push(`writeFileSync sites: ${writeSites}`);
  if (mkdirTotal !== 3 || mkdirBound !== 3) hits.push(`mkdirSync bound to approved dir target: ${mkdirBound}/${mkdirTotal}`);
  if (dirRefs !== 2) hits.push(`sidecar dir refs: ${dirRefs}`);
  if (/fs\.(appendFileSync|createWriteStream|rmSync|unlinkSync|renameSync|copyFileSync)\b/.test(code)) hits.push('unapproved write API');
  if (/fs\.writeFile\s*\(/.test(code)) hits.push('unapproved write API: fs.writeFile');
  if (/fs\.promises\.writeFile\b/.test(code)) hits.push('unapproved write API: fs.promises.writeFile');
  if (/fs\.mkdir\s*\(/.test(code)) hits.push('unapproved mkdir API: fs.mkdir');
  if (!code.includes('fs.writeFileSync(path.join(dir, AUTH_NOTIFY_SIDECAR), JSON.stringify(state), "utf8")')) hits.push('auth sidecar write target missing');
  if (!code.includes('fs.writeFileSync(path.join(dir, `auth-notify-${Date.now()}.json`)')) hits.push('auth notification write target missing');
  if (!code.includes('fs.writeFileSync(path.join(dir, `repair-${Date.now()}.json`)')) hits.push('repair request write target missing');
  return hits;
}

// R28 P1-1：写守卫持久自检——每次质量门在内存中重放变异（目录目标同数替换、
// 第四写、writeFile 旁路、通知目标同数替换），全红才通过。
async function checkWriteGuardSelfTests() {
  const code = await readFile(path.join(CONSOLE_SRC, 'register.js'), 'utf8');
  const current = registerWriteGuardHits(code);
  record('write guard self-test: current register.js clean', current.length === 0, current.join(', ') || 'clean');
  const mkdirSwap = registerWriteGuardHits(code.replace('fs.mkdirSync(dir, { recursive: true })', 'fs.mkdirSync("/tmp/unapproved", { recursive: true })'));
  record('write guard self-test: same-count mkdir target swap is red', mkdirSwap.length > 0);
  record('write guard self-test: fourth writeFileSync is red', registerWriteGuardHits(`${code}\nfs.writeFileSync("/tmp/unapproved.json", "x");`).length > 0);
  record('write guard self-test: fs.writeFile bypass is red', registerWriteGuardHits(`${code}\nfs.writeFile("/tmp/unapproved.json", "x", () => {});`).length > 0);
  record('write guard self-test: fs.promises.writeFile bypass is red', registerWriteGuardHits(`${code}\nfs.promises.writeFile("/tmp/unapproved.json", "x");`).length > 0);
  const targetSwap = registerWriteGuardHits(code.replace('fs.writeFileSync(path.join(dir, `auth-notify-${Date.now()}.json`)', 'fs.writeFileSync("/tmp/unapproved.json"'));
  record('write guard self-test: same-count notification target swap is red', targetSwap.length > 0);
}

async function checkSideEffectGuards() {
  const targets = [path.join(ROOT, 'src', 'main.js'), ...CONSOLE_MODULES.map((n) => path.join(CONSOLE_SRC, n))];
  for (const file of targets) {
    const rel = path.relative(ROOT, file);
    if (!existsSync(file)) {
      record(`side-effect guard: ${rel}`, false, 'file missing');
      continue;
    }
    const code = await readFile(file, 'utf8');
    if (path.basename(file) === CONTROL_CLIENT) {
      // The P2 control client is the single POST-capable module. Every other
      // guard still applies, every URL in it must be the frozen loopback
      // Control API endpoint, POST must appear at exactly one call site, and
      // that call site must be /intents only.
      const controlHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      const foreignUrls = urls.filter((u) => !u.startsWith('http://127.0.0.1:5680/control/v1'));
      if (foreignUrls.length) controlHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) controlHits.push('baseUrl override still present');
      const postCalls = [...code.matchAll(/requestOnce\(\s*"POST"\s*,\s*"([^"]+)"/g)].map((m) => m[1]);
      if (postCalls.length !== 1 || postCalls[0] !== '/intents') {
        controlHits.push(`write paths: ${JSON.stringify(postCalls)}`);
      }
      record(`control guard: ${rel}`, controlHits.length === 0, controlHits.length ? controlHits.join(', ') : 'POST limited to loopback 5680 /intents');
      record('control client targets loopback 5680', code.includes('http://127.0.0.1:5680/control/v1'));
      record('control client sends trusted Obsidian origin', code.includes('app://obsidian.md'));
      record('control client sends intent header', code.includes('X-Loop-Control-Intent'));
      continue;
    }
    if (path.basename(file) === COGNITIVE_DECISION_CLIENT) {
      const cognitiveHits = cognitiveClientGuardHits(code);
      record(
        `cognitive decision guard: ${rel}`,
        cognitiveHits.length === 0,
        cognitiveHits.length
          ? cognitiveHits.join(', ')
          : 'POST limited to loopback 5684 /decisions + /withdrawals',
      );
      record(
        'cognitive decision client targets loopback 5684',
        code.includes('http://127.0.0.1:5684/fragment-cognitive/v1'),
      );
      record(
        'cognitive decision client sends trusted Obsidian origin',
        code.includes('app://obsidian.md'),
      );
      record(
        'cognitive decision client sends decision header',
        code.includes('X-Fragment-Cognitive-Decision'),
      );
      record(
        'cognitive decision client sends withdrawal header',
        code.includes('X-Fragment-Cognitive-Withdrawal'),
      );
      continue;
    }
    if (path.basename(file) === PRODUCT_REVIEW_CLIENT) {
      const productHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      const foreignUrls = urls.filter(
        (url) => !url.startsWith('http://127.0.0.1:5684/fragment/v1/product-reviews'),
      );
      if (foreignUrls.length) productHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) productHits.push('baseUrl override still present');
      if ((code.match(/method:\s*"POST"/g) || []).length !== 1) {
        productHits.push('product POST call site count changed');
      }
      if (!code.includes('"decisions"') || !code.includes('"withdrawals"')) {
        productHits.push('frozen product resources missing');
      }
      record(
        `product review guard: ${rel}`,
        productHits.length === 0,
        productHits.length ? productHits.join(', ') : 'POST limited to loopback product decisions and withdrawals',
      );
      record('product review client targets loopback 5684', code.includes('http://127.0.0.1:5684/fragment/v1/product-reviews'));
      record('product review client sends trusted Obsidian origin', code.includes('app://obsidian.md'));
      record('product review client sends decision header', code.includes('X-Loop-Product-Decision'));
      record('product review client sends withdrawal header', code.includes('X-Loop-Product-Withdrawal'));
      continue;
    }
    if (path.basename(file) === REVIEW_CLIENT) {
      // The P3C review client is the only other POST-capable module, and its
      // writes are review decisions only: frozen loopback 5682, POST at
      // exactly one call site on /decisions, no baseUrl override, trusted
      // Origin and intent header on every write.
      const reviewHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      const foreignUrls = urls.filter((u) => !u.startsWith('http://127.0.0.1:5682/review/v1'));
      if (foreignUrls.length) reviewHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) reviewHits.push('baseUrl override still present');
      const postCalls = [...code.matchAll(/requestOnce\(\s*"POST"\s*,\s*"([^"]+)"/g)].map((m) => m[1]);
      if (postCalls.length !== 1 || postCalls[0] !== '/decisions') {
        reviewHits.push(`write paths: ${JSON.stringify(postCalls)}`);
      }
      record(`review guard: ${rel}`, reviewHits.length === 0, reviewHits.length ? reviewHits.join(', ') : 'POST limited to loopback 5682 /decisions');
      record('review client targets loopback 5682', code.includes('http://127.0.0.1:5682/review/v1'));
      record('review client sends trusted Obsidian origin', code.includes('app://obsidian.md'));
      record('review client sends intent header', code.includes('X-Loop-Review-Intent'));
      continue;
    }
    if (path.basename(file) === CONTINUATION_CLIENT) {
      const continuationHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      const foreignUrls = urls.filter(
        (url) => url !== 'http://127.0.0.1:5684/fragment/v1/continuations',
      );
      if (foreignUrls.length) continuationHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) continuationHits.push('baseUrl override still present');
      const postSites = (code.match(/method:\s*"POST"/g) || []).length;
      if (postSites !== 1) continuationHits.push(`POST sites: ${postSites}`);
      if (!code.includes('[CONTINUATION_HEADER]: "1"')) {
        continuationHits.push('continuation POST missing X-Fragment-Continuation header');
      }
      record(
        `fragment continuation guard: ${rel}`,
        continuationHits.length === 0,
        continuationHits.length ? continuationHits.join(', ') : 'POST limited to loopback 5684 continuation',
      );
      continue;
    }
    if (path.basename(file) === FRAGMENT_INTENT_CLIENT) {
      const intentHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      // Fragment Governed Research v1：除 fragment/v1 对齐资源外，允许唯一的
      // Research Creation Bridge 写资源 /graph/v1/research-runs（DESIGN §5.3）。
      const foreignUrls = urls.filter(
        (url) => !url.startsWith('http://127.0.0.1:5684/fragment/v1/') &&
          url !== 'http://127.0.0.1:5684/graph/v1/research-runs',
      );
      if (foreignUrls.length) intentHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) intentHits.push('baseUrl override still present');
      const postSites = (code.match(/method:\s*"POST"/g) || []).length;
      if (postSites !== 5) intentHits.push(`POST sites: ${postSites}`);
      if (!code.includes('[ALIGNMENT_HEADER]: "1"')) {
        intentHits.push('proposal POST missing X-Fragment-Alignment header');
      }
      if (!code.includes('[DECISION_HEADER]: "1"')) {
        intentHits.push('decision POST missing X-Fragment-Alignment-Decision header');
      }
      if (!code.includes('[CONTINUATION_HEADER]: "1"')) {
        intentHits.push('continuation POST missing X-Fragment-Episode-Continuation header');
      }
      if (!code.includes('[ESCALATION_HEADER]: "1"')) {
        intentHits.push('escalation POST missing X-Fragment-Alignment-Escalation header');
      }
      if (!code.includes('[RESEARCH_RUN_CREATE_HEADER]: "1"')) {
        intentHits.push('research run create POST missing X-Graph-Research-Run-Create header');
      }
      if (!code.includes('/decisions') || !code.includes('/continuations') || !code.includes('/escalations') || !code.includes('/research-runs')) {
        intentHits.push('frozen intent resources missing');
      }
      record(
        `fragment intent guard: ${rel}`,
        intentHits.length === 0,
        intentHits.length ? intentHits.join(', ') : 'POST limited to loopback alignment, continuation, escalation and research run create',
      );
      continue;
    }
    if (path.basename(file) === GRAPH_CLIENT) {
      // Graph Phase 1 + Pilot Entry Bridge v1: the graph client may POST exactly
      // the four frozen bound resources under loopback 5684 /graph/v1 —
      // human-decisions, node reopen, pilot run create and authorization
      // receipt — one call site each, each with its own frozen header. Any
      // other POST, any foreign URL, or a baseUrl override fails the guard.
      const graphHits = FORBIDDEN_PATTERNS.filter(
        (p) => p.name !== 'non-GET HTTP method' && p.re.test(code),
      ).map((p) => p.name);
      const urls = code.match(/https?:\/\/[^\s"'`)\]]+/g) || [];
      const foreignUrls = urls.filter(
        (url) => !url.startsWith('http://127.0.0.1:5684/graph/v1'),
      );
      if (foreignUrls.length) graphHits.push(`foreign URL: ${foreignUrls.join(', ')}`);
      if (/options\.baseUrl/.test(code)) graphHits.push('baseUrl override still present');
      const postSites = (code.match(/method:\s*"POST"/g) || []).length;
      if (postSites !== 6) graphHits.push(`POST sites: ${postSites}`);
      if (!code.includes('/human-decisions') || !code.includes('/reopen')) {
        graphHits.push('frozen graph write resources missing');
      }
      if (!code.includes('/authorization-receipt') || !code.includes('pilot-plans/fragment-pilot-v1') || !code.includes('/pilot-retry')) {
        graphHits.push('frozen pilot write resources missing');
      }
      if (!code.includes('/research-authorization-receipt')) {
        graphHits.push('frozen research receipt resource missing');
      }
      if (!code.includes('[DECISION_HEADER]: "1"')) {
        graphHits.push('graph decision POST missing X-Graph-Human-Decision header');
      }
      if (!code.includes('[REOPEN_HEADER]: "1"')) {
        graphHits.push('graph reopen POST missing X-Graph-Node-Reopen header');
      }
      if (!code.includes('[RUN_CREATE_HEADER]: "1"')) {
        graphHits.push('graph run create POST missing X-Graph-Run-Create header');
      }
      if (!code.includes('[RECEIPT_HEADER]: "1"')) {
        graphHits.push('graph receipt POST missing X-Graph-Authorization-Receipt header');
      }
      if (!code.includes('[RETRY_HEADER]: "1"')) {
        graphHits.push('graph retry POST missing X-Graph-Pilot-Retry header');
      }
      if (!code.includes('[RESEARCH_RECEIPT_HEADER]: "1"')) {
        graphHits.push('graph research receipt POST missing frozen header');
      }
      record(
        `graph client guard: ${rel}`,
        graphHits.length === 0,
        graphHits.length ? graphHits.join(', ') : 'POST limited to six frozen loopback 5684 graph resources',
      );
      record('graph client targets loopback 5684 graph API', code.includes('http://127.0.0.1:5684/graph/v1'));
      record('graph client sends trusted Obsidian origin', code.includes('app://obsidian.md'));
      record('graph client sends decision header', code.includes('X-Graph-Human-Decision'));
      record('graph client sends reopen header', code.includes('X-Graph-Node-Reopen'));
      continue;
    }
    if (path.basename(file) === 'register.js') {
      // R26 P2-2 / R28 P1-1（gate_2dadfb90d539）：已批准 sidecar 写的守卫逻辑
      // 在导出的 registerWriteGuardHits；自检块每次门禁实跑变异反例。
      const writeHits = registerWriteGuardHits(code);
      record(`write allowlist guard: ${rel}`, writeHits.length === 0,
        writeHits.length ? writeHits.join(', ') : '3 approved write sites bound to authNotify x2 + repair x1 targets');
    }
    const hits = FORBIDDEN_PATTERNS.filter((p) => p.re.test(code)).map((p) => p.name);
    if (/127\.0\.0\.1:5680/.test(code)) hits.push('control API 5680 reference outside control client');
    record(`side-effect guard: ${rel}`, hits.length === 0, hits.length ? hits.join(', ') : 'clean');
  }
  const apiClient = await readFile(path.join(CONSOLE_SRC, 'api-client.js'), 'utf8');
  record('api client hardcodes GET', apiClient.includes('method: "GET"'), 'method: "GET" present');
  const registerSrc = await readFile(path.join(CONSOLE_SRC, 'register.js'), 'utf8');
  record('console registers open-loop-console command', registerSrc.includes('id: "open-loop-console"'));
  record('console registers open-graph-workflow command', registerSrc.includes('id: "open-graph-workflow"'));
  const graphViewSrc = await readFile(path.join(CONSOLE_SRC, 'graph-view.js'), 'utf8');
  record(
    'console registers graph workflow view type',
    registerSrc.includes('GRAPH_WORKFLOW_VIEW_TYPE') && graphViewSrc.includes('"my-life-graph-workflow"'),
  );
  record('loop console base URL is loopback 5679', apiClient.includes('http://127.0.0.1:5679/loop/v1'));
  // P3B: the shadow proposal client is GET-only and pinned to loopback 5681;
  // it must never reference the 5680 control API (covered by the guard above).
  const proposalClient = await readFile(path.join(CONSOLE_SRC, 'proposal-client.js'), 'utf8');
  record('proposal client hardcodes GET', proposalClient.includes('method: "GET"'), 'method: "GET" present');
  record('proposal client targets loopback 5681', proposalClient.includes('http://127.0.0.1:5681/shadow/v1'));
  record('proposal client has no baseUrl override', !/options\.baseUrl/.test(proposalClient), 'caller-supplied base URL must be impossible');
}

async function checkFixtures() {
  let entries = [];
  try {
    entries = (await readdir(FIXTURES)).filter((e) => e.endsWith('.json')).sort();
  } catch (err) {
    record('synthetic fixtures readable', false, err.message);
    return;
  }
  for (const name of entries) {
    try {
      const body = JSON.parse(await readFile(path.join(FIXTURES, name), 'utf8'));
      const envelopeOk = body._fixture_type === 'synthetic'
        && body.contract_version === '2'
        && body.db_mode === 'read_only'
        && typeof body.generated_at === 'string'
        && typeof body.provider_version === 'string'
        && typeof body.stale === 'boolean';
      record(`synthetic fixture ${name}`, envelopeOk, envelopeOk ? 'synthetic, contract v2 envelope' : 'envelope violation');
    } catch (err) {
      record(`synthetic fixture ${name}`, false, err.message);
    }
  }
}

async function checkBaselineConsistency() {
  for (const name of FILES) {
    try {
      const srcBuf = await readFile(path.join(SRC, name));
      const baseBuf = await readFile(path.join(BASE, name));
      record(`src/runtime/${name} == baseline`, srcBuf.equals(baseBuf), sha256(srcBuf));
    } catch (err) {
      record(`src/runtime/${name} == baseline`, false, err.message);
    }
  }
  try {
    const mainSrc = await readFile(path.join(SRC, 'main.js'), 'utf8');
    record('homepage path preserved', mainSrc.includes('const HOME_PATH = "Notes/My Life.md";'));
    record('command open-my-life preserved', mainSrc.includes('id: "open-my-life"'));
    record('n8n fragment webhook preserved', mainSrc.includes('http://127.0.0.1:5678/webhook/fragment-organize-now'));
  } catch (err) {
    record('content invariants readable', false, err.message);
  }
  if (!existsSync(DIST)) {
    console.log('note: dist/ not present; skipping dist checks (run npm run build)');
    return;
  }
  const distChecks = [];
  const manifestPath = path.join(DIST, 'manifest.json');
  if (existsSync(manifestPath)) {
    const distBuf = await readFile(manifestPath);
    const baseBuf = await readFile(path.join(BASE, 'manifest.json'));
    distChecks.push(['dist/manifest.json == baseline (byte-identical)', distBuf.equals(baseBuf), sha256(distBuf)]);
  } else {
    distChecks.push(['dist/manifest.json == baseline (byte-identical)', false, 'missing']);
  }
  const stylesPath = path.join(DIST, 'styles.css');
  if (existsSync(stylesPath)) {
    const distCss = await readFile(stylesPath, 'utf8');
    const baseCss = await readFile(path.join(BASE, 'styles.css'), 'utf8');
    const consoleCss = await readFile(path.join(CONSOLE_SRC, 'console.css'), 'utf8');
    const expected = baseCss + '\n' + CSS_MARKER_BEGIN + '\n' + consoleCss + '\n' + CSS_MARKER_END + '\n';
    distChecks.push(['dist/styles.css = baseline + marked console section', distCss === expected, `${Buffer.byteLength(distCss)} bytes`]);
    const beginCount = distCss.split(CSS_MARKER_BEGIN).length - 1;
    const endCount = distCss.split(CSS_MARKER_END).length - 1;
    distChecks.push(['console CSS section appended exactly once', beginCount === 1 && endCount === 1, `begin=${beginCount} end=${endCount}`]);
  } else {
    distChecks.push(['dist/styles.css = baseline + marked console section', false, 'missing']);
  }
  const mainPath = path.join(DIST, 'main.js');
  if (existsSync(mainPath)) {
    const distMain = await readFile(mainPath, 'utf8');
    distChecks.push(['dist/main.js keeps homepage path', distMain.includes('Notes/My Life.md')]);
    distChecks.push(['dist/main.js keeps open-my-life', distMain.includes('open-my-life')]);
    distChecks.push(['dist/main.js keeps n8n webhook', distMain.includes('http://127.0.0.1:5678/webhook/fragment-organize-now')]);
    distChecks.push(['dist/main.js keeps content-state capability', distMain.includes('setContentState')]);
    distChecks.push(['dist/main.js keeps feedback capability', distMain.includes('saveExperimentFeedback')]);
    distChecks.push(['dist/main.js keeps capture capability', distMain.includes('quickCapture')]);
    distChecks.push(['dist/main.js keeps assessment capability', distMain.includes('createProjectAssessment')]);
    distChecks.push(['dist/main.js registers open-loop-console', distMain.includes('open-loop-console')]);
    distChecks.push(['dist/main.js registers console view type', distMain.includes('my-life-loop-console')]);
    distChecks.push(['dist/main.js targets loopback 5679 API', distMain.includes('http://127.0.0.1:5679/loop/v1')]);
    distChecks.push(['dist/main.js targets loopback 5681 shadow API', distMain.includes('http://127.0.0.1:5681/shadow/v1')]);
    distChecks.push(['dist/main.js targets loopback 5682 review API', distMain.includes('http://127.0.0.1:5682/review/v1')]);
    distChecks.push(['dist/main.js targets loopback 5684 cognitive decision API', distMain.includes('http://127.0.0.1:5684/fragment-cognitive/v1')]);
  } else {
    distChecks.push(['dist/main.js present', false, 'missing']);
  }
  for (const [name, ok, detail] of distChecks) record(name, ok, detail);
  const extra = (await readdir(DIST)).filter((e) => !FILES.includes(e));
  record('dist has no extra files', extra.length === 0, extra.join(', '));
}

async function main() {
  await checkPackageJson();
  await checkManifest();
  await checkSyntax();
  await checkSideEffectGuards();
  await checkWriteGuardSelfTests();
  await checkFixtures();
  await checkBaselineConsistency();

  let failures = 0;
  for (const r of results) {
    console.log(`${r.ok ? 'ok  ' : 'FAIL'}  ${r.name}${r.detail ? `  (${r.detail})` : ''}`);
    if (!r.ok) failures += 1;
  }
  console.log(`${results.length - failures}/${results.length} checks passed`);
  if (failures > 0) process.exit(1);
}

const invokedAs = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedAs) {
  main().catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}
