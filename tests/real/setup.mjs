/**
 * Stand up a real Audiobookshelf, seed it, and record how to reach it.
 *
 * The stub in tests/e2e is written from our own assumptions, so it can only
 * ever confirm them. It did: the in-page UI was broken against every stock
 * install for two separate reasons the stub could not express - the library
 * page fetches /personalized (a bare array of shelves, not {results}), and
 * card ids repeat because every shelf numbers its own from zero. Both were
 * found by running the real thing, so CI runs the real thing.
 *
 * Writes tests/real/state.json. The spec skips itself when that is absent, so
 * `npx playwright test` stays useful without a build.
 *
 *   node tests/real/setup.mjs            # build, start, seed
 *   ABS_REF=main node tests/real/setup.mjs
 */
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, rmSync, openSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const work = process.env.ABS_WORK || join(root, ".abs-real");
// Pinned: a moving target turns an upstream change into a red PR on unrelated
// work. .github/workflows/upstream.yml is where new releases get noticed.
const REF = process.env.ABS_REF || "v2.36.0";
const PORT = Number(process.env.ABS_PORT || 13378);
const USER = "root";
const PASS = "testpass123";

const run = (cmd, args, opts = {}) =>
  execFileSync(cmd, args, { stdio: "inherit", ...opts });

const src = join(work, "abs");
const data = join(work, "data");
const device = join(work, "device");

if (!existsSync(join(src, "package.json"))) {
  rmSync(src, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  console.log(`> cloning audiobookshelf ${REF}`);
  run("git", ["clone", "--depth", "1", "--branch", REF,
              "https://github.com/advplyr/audiobookshelf", src]);
}

if (!existsSync(join(src, "node_modules"))) {
  console.log("> installing server deps");
  run("npm", ["ci", "--no-audit", "--no-fund"], { cwd: src });
}
if (!existsSync(join(src, "client", "node_modules"))) {
  console.log("> installing client deps");
  run("npm", ["ci", "--no-audit", "--no-fund"], { cwd: join(src, "client") });
}
if (!existsSync(join(src, "client", "dist", "200.html"))) {
  console.log("> building client (nuxt generate)");
  run("npm", ["run", "generate"], {
    cwd: join(src, "client"),
    env: { ...process.env, NODE_OPTIONS: "--openssl-legacy-provider" }
  });
}

// Audiobookshelf downloads ffmpeg/ffprobe from GitHub releases on first run,
// which a locked-down network blocks. These ship as npm packages instead.
const ff = join(work, "ffbin");
if (!existsSync(join(ff, "node_modules"))) {
  mkdirSync(ff, { recursive: true });
  writeFileSync(join(ff, "package.json"), '{"name":"ffbin","private":true}\n');
  console.log("> installing ffmpeg/ffprobe");
  run("npm", ["i", "--no-audit", "--no-fund",
              "@ffmpeg-installer/ffmpeg", "@ffprobe-installer/ffprobe"], { cwd: ff });
}
const FFMPEG = join(ff, "node_modules/@ffmpeg-installer/linux-x64/ffmpeg");
const FFPROBE = join(ff, "node_modules/@ffprobe-installer/linux-x64/ffprobe");

for (const d of [join(data, "config"), join(data, "metadata"),
                 join(data, "books/Brian Jacques/Redwall"),
                 join(data, "books/Louis Sachar/Holes"), device]) {
  mkdirSync(d, { recursive: true });
}

const book = (dir, title, author) => {
  const out = join(data, "books", dir, `${title}.m4b`);
  if (existsSync(out)) return;
  execFileSync(FFMPEG, ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
    "-c:a", "aac", "-b:a", "32k",
    "-metadata", `title=${title}`, "-metadata", `artist=${author}`,
    "-metadata", `album=${title}`, "-metadata", `album_artist=${author}`,
    "-y", out], { stdio: "ignore" });
};
book("Brian Jacques/Redwall", "Redwall", "Brian Jacques");
book("Louis Sachar/Holes", "Holes", "Louis Sachar");

console.log("> starting server");
// Log to a file rather than pipes: an inherited pipe keeps this process's
// event loop alive, so the script would seed the server and then hang forever
// instead of exiting for the test run that follows.
const logFd = openSync(join(work, "server.log"), "a");
const server = spawn(process.execPath, ["index.js"], {
  cwd: src,
  detached: true,
  stdio: ["ignore", logFd, logFd],
  env: {
    ...process.env,
    PORT: String(PORT),
    SKIP_BINARIES_CHECK: "1",
    FFMPEG_PATH: FFMPEG,
    FFPROBE_PATH: FFPROBE,
    CONFIG_PATH: join(data, "config"),
    METADATA_PATH: join(data, "metadata"),
  },
});
server.unref();

// ROUTER_BASE_PATH defaults to /audiobookshelf in Audiobookshelf's own
// index.js, so this prefix is the stock deployment, not a proxy quirk.
const BASE = process.env.ROUTER_BASE_PATH ?? "/audiobookshelf";
const abs = `http://127.0.0.1:${PORT}${BASE}`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (path, opts = {}) => {
  const r = await fetch(`${abs}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const text = await r.text();
  try { return { status: r.status, body: JSON.parse(text) }; }
  catch { return { status: r.status, body: text }; }
};

let up = false;
for (let i = 0; i < 90; i++) {
  try {
    const r = await api("/status");
    if (r.status === 200) { up = true; break; }
  } catch { /* not listening yet */ }
  await sleep(1000);
}
if (!up) { server.kill(); throw new Error("audiobookshelf did not come up"); }

const status = (await api("/status")).body;
if (!status.isInit) {
  console.log("> creating root user");
  await api("/init", { method: "POST",
    body: JSON.stringify({ newRoot: { username: USER, password: PASS } }) });
}

const login = await api("/login", { method: "POST",
  body: JSON.stringify({ username: USER, password: PASS }) });
const token = login.body?.user?.token;
if (!token) { server.kill(); throw new Error(`login failed: ${JSON.stringify(login.body).slice(0, 200)}`); }

const auth = { Authorization: `Bearer ${token}` };
let libs = (await api("/api/libraries", { headers: auth })).body.libraries || [];
if (!libs.length) {
  console.log("> creating library");
  await api("/api/libraries", { method: "POST", headers: auth,
    body: JSON.stringify({ name: "Audiobooks", mediaType: "book",
                           folders: [{ fullPath: join(data, "books") }] }) });
  libs = (await api("/api/libraries", { headers: auth })).body.libraries || [];
}
const libraryId = libs[0].id;

await api(`/api/libraries/${libraryId}/scan`, { method: "POST", headers: auth });
let items = [];
for (let i = 0; i < 30; i++) {
  const r = await api(`/api/libraries/${libraryId}/items?limit=0&minified=1`, { headers: auth });
  items = r.body.results || [];
  if (items.length) break;
  await sleep(1000);
}
if (!items.length) { server.kill(); throw new Error("scan produced no items"); }

const state = {
  absUrl: abs, token, libraryId, device, pid: server.pid,
  username: USER, password: PASS, serverVersion: status.serverVersion,
  titles: items.map((i) => i.media?.metadata?.title).filter(Boolean).sort(),
};
writeFileSync(join(here, "state.json"), JSON.stringify(state, null, 2) + "\n");
console.log(`> ready: ${abs} (v${status.serverVersion}), ${items.length} items, pid ${server.pid}`);
