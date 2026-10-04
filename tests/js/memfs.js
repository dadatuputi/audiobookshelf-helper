/* An in-memory FileSystemDirectoryHandle, for running folder.js under node.
 *
 * Only the surface folder.js uses, with the errors Chromium throws: a
 * NotFoundError for something absent, a TypeMismatchError for a file asked
 * for as a folder or the other way round, and a TypeError for a name the API
 * refuses. tests/e2e/folder.spec.js runs the same vectors against a real
 * browser filesystem (the origin private file system), so this fake is the
 * fast check, not the only one. */

const invalid = (name) =>
  typeof name !== "string" || !name || name === "." || name === ".." || /[/\\]/.test(name);

function notFound(name) {
  return new DOMException(`A requested file or directory could not be found: ${name}`, "NotFoundError");
}

function mismatch(name) {
  return new DOMException(`The path supplied exists, but was not an entry of requested type: ${name}`,
                          "TypeMismatchError");
}

class MemWritable extends WritableStream {
  constructor(file) {
    const chunks = [];
    super({
      write(c) { chunks.push(c); },
      // Like the real one: nothing is visible until close() swaps it in.
      close() { file.blob = new Blob(chunks); },
      abort() { chunks.length = 0; },
    });
  }

  async write(data) {
    const w = this.getWriter();
    try { await w.write(data); } finally { w.releaseLock(); }
  }
}

export class MemFile {
  constructor(name, blob = new Blob([])) {
    this.kind = "file";
    this.name = name;
    this.blob = blob;
  }

  async getFile() {
    return new File([this.blob], this.name);
  }

  async createWritable() {
    return new MemWritable(this);
  }

  async queryPermission() { return "granted"; }
}

export class MemDir {
  constructor(name = "device") {
    this.kind = "directory";
    this.name = name;
    this.kids = new Map();
  }

  async *entries() {
    // A snapshot, so removing while iterating behaves.
    for (const [k, v] of [...this.kids]) yield [k, v];
  }

  async *keys() {
    for (const k of [...this.kids.keys()]) yield k;
  }

  async getDirectoryHandle(name, { create = false } = {}) {
    if (invalid(name)) throw new TypeError(`Name is not allowed: ${name}`);
    const got = this.kids.get(name);
    if (got) {
      if (got.kind !== "directory") throw mismatch(name);
      return got;
    }
    if (!create) throw notFound(name);
    const d = new MemDir(name);
    this.kids.set(name, d);
    return d;
  }

  async getFileHandle(name, { create = false } = {}) {
    if (invalid(name)) throw new TypeError(`Name is not allowed: ${name}`);
    const got = this.kids.get(name);
    if (got) {
      if (got.kind !== "file") throw mismatch(name);
      return got;
    }
    if (!create) throw notFound(name);
    const f = new MemFile(name);
    this.kids.set(name, f);
    return f;
  }

  async removeEntry(name, { recursive = false } = {}) {
    if (invalid(name)) throw new TypeError(`Name is not allowed: ${name}`);
    const got = this.kids.get(name);
    if (!got) throw notFound(name);
    if (got.kind === "directory" && got.kids.size && !recursive) {
      throw new DOMException("The directory is not empty", "InvalidModificationError");
    }
    this.kids.delete(name);
  }

  async queryPermission() { return "granted"; }
}

/** Lay a parity fixture's tree out: {"a/b.mp3": base64 | null (a folder)}. */
export async function buildTree(dir, tree) {
  for (const rel of Object.keys(tree).sort()) {
    const parts = rel.split("/");
    let d = dir;
    const last = tree[rel] === null ? parts.length : parts.length - 1;
    for (const p of parts.slice(0, last)) d = await d.getDirectoryHandle(p, { create: true });
    if (tree[rel] !== null) {
      const f = await d.getFileHandle(parts[parts.length - 1], { create: true });
      f.blob = new Blob([Buffer.from(tree[rel], "base64")]);
    }
  }
}
