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
  // Audiobookshelf is commonly reached through a reverse proxy that puts it
  // under a path. The browser URL then carries that prefix whether or not the
  // server itself is configured with a base path - and dropping it here
  // registered the content script for a URL that never matches, so the whole
  // in-page UI was silently absent with no error anywhere.
  it("keeps the path the server is reached under", () => {
    expect(ABSH.libraryPattern("https://h/audiobookshelf"))
      .toBe("https://h/audiobookshelf/library/*");
  });

  it("keeps it with a trailing slash too", () => {
    expect(ABSH.libraryPattern("https://h/audiobookshelf/"))
      .toBe("https://h/audiobookshelf/library/*");
  });

  it("handles more than one path segment", () => {
    expect(ABSH.libraryPattern("https://h/media/abs")).toBe("https://h/media/abs/library/*");
  });

  it("adds nothing when the server is at the root", () => {
    expect(ABSH.libraryPattern("http://h:13378")).toBe("http://h:13378/library/*");
  });


  it("scopes the content script to that server's library pages", () => {
    expect(ABSH.libraryPattern("http://media.local:13378/")).toBe("http://media.local:13378/library/*");
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
