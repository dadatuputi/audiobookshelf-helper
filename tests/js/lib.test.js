import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
let ABSH;

beforeAll(() => {
  // lib.js is a classic script; run it in a sandbox and grab the export.
  const code = readFileSync(resolve(here, "../../extension/src/lib.js"), "utf8");
  // A bare vm context has the JS builtins but no web APIs; originPattern
  // parses with URL, so hand it in.
  const sandbox = { module: { exports: {} }, globalThis: {}, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  ABSH = sandbox.module.exports;
});

describe("baseUrl", () => {
  it("strips trailing slashes", () => {
    expect(ABSH.baseUrl("http://x:13378/")).toBe("http://x:13378");
    expect(ABSH.baseUrl("http://x:13378///")).toBe("http://x:13378");
  });
  it("leaves a clean url alone", () => {
    expect(ABSH.baseUrl("http://x:13378")).toBe("http://x:13378");
  });
  it("tolerates empty input", () => {
    expect(ABSH.baseUrl(undefined)).toBe("");
  });
});

describe("downloadUrl", () => {
  it("puts the token in the query string, not a header", () => {
    const u = ABSH.downloadUrl("http://x:13378", "abc", "tok");
    expect(u).toBe("http://x:13378/api/items/abc/download?token=tok");
  });
  it("percent-encodes the token", () => {
    expect(ABSH.downloadUrl("http://x", "i", "a b/c")).toContain("token=a%20b%2Fc");
  });
  it("percent-encodes the item id", () => {
    expect(ABSH.downloadUrl("http://x", "a/b", "t")).toContain("/api/items/a%2Fb/download");
  });
  it("rejects a missing server url", () => {
    expect(() => ABSH.downloadUrl("", "i", "t")).toThrow(/server URL/);
  });
  it("rejects a missing item id", () => {
    expect(() => ABSH.downloadUrl("http://x", "", "t")).toThrow(/item id/);
  });
});

describe("normalizeBook", () => {
  it("reads title, author and series from nested metadata", () => {
    const b = ABSH.normalizeBook({
      id: "1", relPath: "A/B", size: 10,
      media: {
        numTracks: 3,
        metadata: {
          title: "T", authorName: "Auth",
          series: [{ name: "S1" }, { name: "S2" }]
        }
      }
    });
    expect(b).toMatchObject({ id: "1", title: "T", author: "Auth", series: "S1, S2", numTracks: 3 });
  });

  it("falls back to the authors array when authorName is absent", () => {
    const b = ABSH.normalizeBook({
      id: "2", media: { metadata: { title: "T", authors: [{ name: "X" }, { name: "Y" }] } }
    });
    expect(b.author).toBe("X, Y");
  });

  it("falls back to relPath when there is no title", () => {
    expect(ABSH.normalizeBook({ id: "3", relPath: "P/Q", media: {} }).title).toBe("P/Q");
  });

  it("survives a completely empty item", () => {
    const b = ABSH.normalizeBook({ id: "4" });
    expect(b.title).toBe("(untitled)");
    expect(b.author).toBe("");
    expect(b.numTracks).toBe(0);
  });

  it("counts audioFiles when numTracks is missing", () => {
    const b = ABSH.normalizeBook({ id: "5", media: { audioFiles: [{}, {}] } });
    expect(b.numTracks).toBe(2);
  });
});

describe("filterBooks", () => {
  const list = [
    { title: "Redwall", author: "Brian Jacques", series: "Redwall" },
    { title: "Holes", author: "Louis Sachar", series: "" },
    { title: "Starship Troopers", author: "Robert A. Heinlein", series: "" }
  ];
  it("matches on title", () => expect(ABSH.filterBooks(list, "holes")).toHaveLength(1));
  it("matches on author", () => expect(ABSH.filterBooks(list, "heinlein")).toHaveLength(1));
  it("matches on series", () => expect(ABSH.filterBooks(list, "redwall")).toHaveLength(1));
  it("is case insensitive", () => expect(ABSH.filterBooks(list, "HOLES")).toHaveLength(1));
  it("returns everything for an empty query", () => expect(ABSH.filterBooks(list, "  ")).toHaveLength(3));
});

describe("sortBooks", () => {
  it("orders by author then title, without mutating the input", () => {
    const list = [
      { author: "B", title: "z" }, { author: "A", title: "b" }, { author: "A", title: "a" }
    ];
    const copy = JSON.parse(JSON.stringify(list));
    const out = ABSH.sortBooks(list);
    expect(out.map(b => b.author + b.title)).toEqual(["Aa", "Ab", "Bz"]);
    expect(list).toEqual(copy);
  });
});

describe("buildSyncPayload", () => {
  const cfg = {
    absUrl: "http://x:13378/", apiKey: "k", devicePath: "/Volumes/CLIP",
    subdir: "AUDIOBOOKS", renameM4b: true, folderTemplate: "{author} - {title}",
    sourceMode: "auto", localRoot: "/Volumes/media"
  };
  const items = [{ id: "i1", title: "T", author: "A", series: "S", relPath: "A/T" }];

  it("produces a sync command with a per-item url", () => {
    const p = ABSH.buildSyncPayload(cfg, items);
    expect(p.cmd).toBe("sync");
    expect(p.items[0].url).toBe("http://x:13378/api/items/i1/download?token=k");
  });

  it("carries the device options through", () => {
    const p = ABSH.buildSyncPayload(cfg, items);
    expect(p).toMatchObject({
      devicePath: "/Volumes/CLIP", subdir: "AUDIOBOOKS",
      renameM4b: true, sourceMode: "auto", localRoot: "/Volumes/media"
    });
  });

  it("defaults renameM4b to true when unset", () => {
    const { renameM4b, ...rest } = cfg;
    expect(ABSH.buildSyncPayload(rest, items).renameM4b).toBe(true);
  });

  it("honours renameM4b === false", () => {
    expect(ABSH.buildSyncPayload({ ...cfg, renameM4b: false }, items).renameM4b).toBe(false);
  });

  it("refuses to run without a device path", () => {
    expect(() => ABSH.buildSyncPayload({ ...cfg, devicePath: "" }, items)).toThrow(/device path/);
  });

  it("refuses an empty selection", () => {
    expect(() => ABSH.buildSyncPayload(cfg, [])).toThrow(/no items/);
  });
});

describe("DEFAULTS", () => {
  it("renames m4b by default - the Clip cannot read .m4b", () => {
    expect(ABSH.DEFAULTS.renameM4b).toBe(true);
  });
  it("targets AUDIOBOOKS, which SanDisk players treat specially", () => {
    expect(ABSH.DEFAULTS.subdir).toBe("AUDIOBOOKS");
  });
});

describe("originPattern", () => {
  it("narrows to the configured origin, not every site", () => {
    expect(ABSH.originPattern("http://media.local:13378")).toBe("http://media.local:13378/*");
  });
  it("keeps https and the port", () => {
    expect(ABSH.originPattern("https://abs.example.com:8443/")).toBe("https://abs.example.com:8443/*");
  });
  it("drops any path", () => {
    expect(ABSH.originPattern("http://x:1/library/main")).toBe("http://x:1/*");
  });
  it("rejects a non-http scheme", () => {
    expect(() => ABSH.originPattern("file:///etc")).toThrow(/http/);
  });
  it("rejects junk", () => {
    expect(() => ABSH.originPattern("not a url")).toThrow();
  });
});

describe("libraryPattern", () => {
  it("scopes the content script to that server's library pages", () => {
    expect(ABSH.libraryPattern("http://media.local:13378/")).toBe("http://media.local:13378/library/*");
  });
});

describe("buildListPayload", () => {
  const cfg = { devicePath: "/Volumes/CLIP", subdir: "AUDIOBOOKS", renameM4b: true,
                folderTemplate: "{author} - {title}" };
  it("asks for a list with the device options the host needs to match names", () => {
    const p = ABSH.buildListPayload(cfg, [{ id: "i1", title: "T", author: "A", series: "S" }]);
    expect(p.cmd).toBe("list");
    expect(p.folderTemplate).toBe("{author} - {title}");
    expect(p.devicePath).toBe("/Volumes/CLIP");
    expect(p.items[0]).toEqual({ id: "i1", title: "T", author: "A", series: "S" });
  });
  it("does not leak the download url or api key to a read-only command", () => {
    const p = ABSH.buildListPayload(cfg, [{ id: "i1", title: "T", author: "A", series: "S" }]);
    expect(JSON.stringify(p)).not.toContain("token=");
  });
  it("tolerates an empty library", () => {
    expect(ABSH.buildListPayload(cfg, []).items).toEqual([]);
  });
  it("still refuses without a device path", () => {
    expect(() => ABSH.buildListPayload({ ...cfg, devicePath: "" }, [])).toThrow(/device path/);
  });
});

describe("buildRemovePayload", () => {
  const cfg = { devicePath: "/Volumes/CLIP", subdir: "AUDIOBOOKS" };
  it("carries the names to delete", () => {
    const p = ABSH.buildRemovePayload(cfg, ["A - B.m4a"]);
    expect(p.cmd).toBe("remove");
    expect(p.names).toEqual(["A - B.m4a"]);
  });
  it("copies the array so later mutation cannot change the request", () => {
    const names = ["x"];
    const p = ABSH.buildRemovePayload(cfg, names);
    names.push("y");
    expect(p.names).toEqual(["x"]);
  });
  it("refuses an empty removal", () => {
    expect(() => ABSH.buildRemovePayload(cfg, [])).toThrow(/nothing to remove/);
  });
});

describe("annotateOnDevice", () => {
  const books = [{ id: "a", title: "A" }, { id: "b", title: "B" }];
  it("marks the books the host found", () => {
    const out = ABSH.annotateOnDevice(books, {
      onDevice: [{ id: "a", name: "X.m4a", kind: "file", bytes: 10, files: 1 }]
    });
    expect(out[0].onDevice).toMatchObject({ name: "X.m4a", bytes: 10 });
    expect(out[1].onDevice).toBeNull();
  });
  it("leaves every book unmarked when the device is empty", () => {
    expect(ABSH.annotateOnDevice(books, { onDevice: [] }).every(b => b.onDevice === null)).toBe(true);
  });
  it("survives a missing reply", () => {
    expect(ABSH.annotateOnDevice(books, null).every(b => b.onDevice === null)).toBe(true);
  });
  it("does not mutate the input", () => {
    const copy = JSON.parse(JSON.stringify(books));
    ABSH.annotateOnDevice(books, { onDevice: [{ id: "a", name: "X" }] });
    expect(books).toEqual(copy);
  });
});

describe("formatBytes", () => {
  it("keeps small numbers whole", () => expect(ABSH.formatBytes(512)).toBe("512B"));
  it("uses one decimal below ten units", () => expect(ABSH.formatBytes(1536)).toBe("1.5KB"));
  it("rounds larger values", () => expect(ABSH.formatBytes(120 * 1048576)).toBe("120MB"));
  it("handles zero and junk", () => {
    expect(ABSH.formatBytes(0)).toBe("0B");
    expect(ABSH.formatBytes(undefined)).toBe("0B");
  });
});
