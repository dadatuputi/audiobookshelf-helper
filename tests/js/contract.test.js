/* The Audiobookshelf API shapes this extension depends on.
 *
 * These are the assertions that turn an upstream change from "the book list is
 * mysteriously empty" into a failing build. The fixtures are recorded
 * responses; tests/fixtures/abs/contract.json is the human-readable list of
 * what we rely on, and tools/check_upstream.py quotes it into the issue it
 * opens when Audiobookshelf cuts a release.
 *
 * If something here fails after an ABS upgrade, the fix is in lib.js and the
 * fixture should be re-recorded from the new server - not loosened to pass.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (n) =>
  JSON.parse(readFileSync(resolve(here, "../fixtures/abs", n), "utf8"));

let ABSH;
beforeAll(() => {
  const code = readFileSync(resolve(here, "../../extension/src/lib.js"), "utf8");
  const sandbox = { module: { exports: {} }, globalThis: {}, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  ABSH = sandbox.module.exports;
});

/* Mirrors background.js listLibraries. */
const pickLibraries = (d) =>
  (Array.isArray(d) ? d : d.libraries || [])
    .filter((l) => l.mediaType === "book")
    .map((l) => ({ id: l.id, name: l.name }));

describe("GET /api/libraries", () => {
  const data = fixture("libraries.json");

  it("finds the book libraries and ignores podcasts", () => {
    expect(pickLibraries(data)).toEqual([
      { id: "lib_c1u6t4p45c35rf0nzd", name: "Audiobooks" }
    ]);
  });

  it("still works if the server returns a bare array", () => {
    expect(pickLibraries(data.libraries)).toHaveLength(1);
  });

  it("depends on exactly id, name and mediaType", () => {
    for (const l of data.libraries) {
      expect(l).toHaveProperty("id");
      expect(l).toHaveProperty("name");
      expect(l).toHaveProperty("mediaType");
    }
  });
});

describe("GET /api/libraries/{id}/items - minified form", () => {
  const results = fixture("items-minified.json").results;

  it("normalizes a single-file book", () => {
    const b = ABSH.normalizeBook(results[0]);
    expect(b).toMatchObject({
      id: "li_8gch9ve09orgn4fdz8",
      title: "Redwall",
      author: "Brian Jacques",
      relPath: "Brian Jacques/Redwall",
      numTracks: 1,
      size: 268435456
    });
  });

  it("normalizes a multi-file book", () => {
    const b = ABSH.normalizeBook(results[1]);
    expect(b.numTracks).toBe(12);
    expect(b.title).toBe("Holes");
  });

  it("relPath is present, because the local-share path depends on it", () => {
    for (const r of results) expect(typeof r.relPath).toBe("string");
  });

  it("sorts by author then title", () => {
    const out = ABSH.sortBooks(results.map(ABSH.normalizeBook));
    expect(out.map((b) => b.title)).toEqual(["Redwall", "Holes"]);
  });
});

describe("GET /api/libraries/{id}/items - full form", () => {
  const results = fixture("items-full.json").results;

  it("reads authors from the array when authorName is absent", () => {
    const b = ABSH.normalizeBook(results[0]);
    expect(b.author).toBe("Brian Jacques, Someone Else");
  });

  it("reads series names from the series array", () => {
    expect(ABSH.normalizeBook(results[0]).series).toBe("Redwall");
  });

  it("counts audioFiles when numTracks is absent", () => {
    expect(ABSH.normalizeBook(results[0]).numTracks).toBe(1);
  });
});

describe("GET /api/items/{id}/download", () => {
  it("authenticates with ?token=, which downloads cannot do with a header", () => {
    const u = ABSH.downloadUrl("http://media.local:13378", "li_8gch9ve09orgn4fdz8", "tok");
    expect(u).toBe(
      "http://media.local:13378/api/items/li_8gch9ve09orgn4fdz8/download?token=tok");
  });
});

describe("the recorded contract", () => {
  const contract = fixture("contract.json");

  it("names the upstream project the watcher polls", () => {
    expect(contract.upstream).toBe("advplyr/audiobookshelf");
  });

  it("covers every endpoint the extension calls", () => {
    const paths = contract.endpoints.map((e) => e.path);
    expect(paths).toContain("/api/libraries");
    expect(paths).toContain("/api/libraries/{id}/items?limit=0");
    expect(paths).toContain("/api/items/{id}/download?token={apiKey}");
  });

  it("every endpoint says what it is used for and what it needs", () => {
    for (const e of contract.endpoints) {
      expect(e.used_for).toBeTruthy();
      expect(e.fields.length).toBeGreaterThan(0);
    }
  });
});
