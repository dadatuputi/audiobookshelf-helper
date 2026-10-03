/**
 * Drive one of the add-on's own pages in a real Firefox, over the remote
 * debugging protocol rather than Playwright.
 *
 * Playwright's Firefox (Juggler) commits a navigation to moz-extension:// and
 * then never reports it loaded (microsoft/playwright#3792), so page.fill() and
 * friends have no document to act on. Firefox's own debugger has no such
 * blind spot: a tab descriptor navigates its tab with the address bar's
 * privileges, and the target it hands out evaluates in whatever document the
 * tab holds, extension page or not. So the tab is opened through Playwright,
 * moved to the extension page by its tab descriptor, and driven through that
 * document's console actor.
 *
 * Routes not taken, so nobody re-walks them:
 *  - WebDriver BiDi. Playwright's bundled Firefox is launched with
 *    -juggler-pipe, not --remote-debugging-port; BiDi means a second, stock
 *    Firefox in CI and a WebSocket client, which Node 20 does not ship. RDP is
 *    already what installs the add-on here, so it costs nothing new.
 *  - Patching omni.ja so Juggler accepts extension pages, as DuckDuckGo's
 *    harness does. It edits the browser under test, and has to be redone
 *    against every Firefox Playwright rolls to.
 *
 * What this cannot do is make a trusted event. Clicks and typing are
 * dispatched from page script, so the page's own handlers run exactly as
 * they would for a user, but anything gated on a real user gesture - the
 * Grant access button's permissions.request() - stays out of reach.
 */
import { connect } from "./firefox-addon.mjs";

const REQUEST_TIMEOUT = 15_000;

/** A request the server refused. Carries RDP's own error name, which is the
 *  useful part: noSuchActor and unrecognizedPacketType mean different things. */
export class RdpError extends Error {
  constructor(to, type, reply) {
    super(`RDP ${type} to ${to}: ${reply.error}${reply.message ? ` - ${reply.message}` : ""}`);
    this.error = reply.error;
  }
}

/**
 * A connection that pairs replies by actor, not by arrival.
 *
 * The client in firefox-addon.mjs pairs each packet with the oldest
 * outstanding request, which holds only while nothing speaks unprompted. A
 * console evaluation does: it answers with an id and delivers the result
 * later as an evaluationResult event, and tabs announce navigations
 * (tabListChanged) whenever they like. RDP keeps replies in request order per
 * actor and marks every unsolicited packet with a `type`, so route on that.
 */
export class RdpClient {
  static async open(port) {
    const client = new RdpClient(await connect(port));
    await client.greeting;
    return client;
  }

  constructor(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.queues = new Map();
    this.listeners = new Set();
    this.gone = null;
    this.greeting = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("no RDP greeting within 30s")), 30_000);
      this.onGreeting = (msg) => { clearTimeout(timer); resolve(msg); };
    });
    socket.on("data", (chunk) => this.onData(chunk));
    socket.on("error", (e) => this.fail(e));
    socket.on("close", () => this.fail(new Error("Firefox closed the RDP connection")));
  }

  onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    // `<byte length>:<JSON>`; a packet can span chunks and a chunk can hold
    // several packets.
    for (;;) {
      const colon = this.buffer.indexOf(0x3a);
      if (colon < 0) return;
      const len = Number(this.buffer.subarray(0, colon).toString("ascii"));
      if (!Number.isInteger(len) || len < 0) {
        // Framing is lost and cannot be found again; say so rather than
        // leaving every waiter to time out in silence.
        this.fail(new Error(`RDP framing lost at ${JSON.stringify(
          this.buffer.subarray(0, 40).toString("latin1"))}`));
        this.socket.destroy();
        return;
      }
      const start = colon + 1;
      if (this.buffer.length < start + len) return;
      const body = this.buffer.subarray(start, start + len).toString("utf8");
      this.buffer = this.buffer.subarray(start + len);
      let msg;
      try { msg = JSON.parse(body); } catch { continue; }
      this.dispatch(msg);
    }
  }

  dispatch(msg) {
    if (this.onGreeting) {
      const greet = this.onGreeting;
      this.onGreeting = null;
      greet(msg);
      return;
    }
    if (msg.type !== undefined) {
      for (const fn of this.listeners) fn(msg);
      return;
    }
    const waiter = this.queues.get(msg.from)?.shift();
    if (!waiter) return;
    clearTimeout(waiter.timer);
    // A waiter that timed out keeps its place in the queue, so the reply it
    // was owed is swallowed here instead of being handed to the next request
    // to the same actor - which would then be answered with the wrong packet.
    if (waiter.dead) return;
    if (msg.error) waiter.reject(new RdpError(msg.from, waiter.type, msg));
    else waiter.resolve(msg);
  }

  fail(err) {
    if (this.gone) return;
    this.gone = err;
    for (const queue of this.queues.values()) {
      for (const w of queue) { clearTimeout(w.timer); if (!w.dead) w.reject(err); }
    }
    this.queues.clear();
  }

  request(to, type, params = {}, timeout = REQUEST_TIMEOUT) {
    if (this.gone) return Promise.reject(this.gone);
    return new Promise((resolve, reject) => {
      const waiter = { type, resolve, reject, dead: false };
      waiter.timer = setTimeout(() => {
        waiter.dead = true;
        reject(new Error(`RDP ${type} to ${to}: no reply within ${timeout}ms`));
      }, timeout);
      if (!this.queues.has(to)) this.queues.set(to, []);
      this.queues.get(to).push(waiter);
      const payload = Buffer.from(JSON.stringify({ to, type, ...params }), "utf8");
      this.socket.write(Buffer.concat([Buffer.from(`${payload.length}:`, "ascii"), payload]));
    });
  }

  /** Every unsolicited packet, until the returned function is called. */
  listen(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  close() {
    this.socket.end();
  }
}

/**
 * Evaluate source text in a document and return its result as JSON.
 *
 * The console answers evaluateJSAsync with an id and sends the value later,
 * so listen before asking. The text is wrapped to return a JSON string,
 * because a string is the one result the console hands back by value; an
 * object comes back as a handle to more actors.
 */
async function evaluateText(client, consoleActor, text, timeout = REQUEST_TIMEOUT) {
  const results = new Map();
  let wake = () => {};
  const stop = client.listen((msg) => {
    if (msg.type === "evaluationResult" && msg.from === consoleActor) {
      results.set(msg.resultID, msg);
      wake();
    }
  });
  try {
    const reply = await client.request(consoleActor, "evaluateJSAsync",
                                       { text: `JSON.stringify(${text})` }, timeout);
    let msg = results.get(reply.resultID);
    if (!msg) {
      msg = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(
          `console ${consoleActor} sent no evaluationResult within ${timeout}ms`)), timeout);
        wake = () => {
          const got = results.get(reply.resultID);
          if (got) { clearTimeout(timer); resolve(got); }
        };
      });
    }
    if (msg.hasException || msg.exceptionMessage) {
      const said = typeof msg.exceptionMessage === "string" ? msg.exceptionMessage
        : JSON.stringify(msg.exceptionMessage || msg.exception).slice(0, 300);
      throw new Error(`page threw: ${said}`);
    }
    if (typeof msg.result !== "string") {
      // undefined comes back as a grip; anything else here is a result too
      // long to be sent by value, which nothing in these tests should produce.
      if (msg.result && msg.result.type === "undefined") return undefined;
      throw new Error(`unexpected console result: ${JSON.stringify(msg.result).slice(0, 200)}`);
    }
    return JSON.parse(msg.result);
  } finally {
    stop();
  }
}

/** Poll until fn() returns something truthy, or fail naming what never came.
 *  `what` may be a function, to describe the last thing seen at the moment
 *  of failing rather than at the start. */
async function settle(fn, { timeout, what }) {
  const deadline = Date.now() + timeout;
  let last;
  for (;;) {
    try {
      const v = await fn();
      if (v) return v;
      last = v;
    } catch (e) {
      last = e;
    }
    if (Date.now() > deadline) {
      const why = last instanceof Error ? last.message : JSON.stringify(last);
      const said = typeof what === "function" ? what() : what;
      throw new Error(`${said} did not happen within ${timeout}ms (last: ${why})`);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

let nextSlot = 0;

/** One tab, showing one extension page, reached through its console actor. */
export class ExtensionPage {
  constructor(client, descriptor, url) {
    this.client = client;
    this.descriptor = descriptor;
    this.url = url;
    this.consoleActor = null;
  }

  /**
   * Find the document now in the tab and bind to its console.
   *
   * A target belongs to one document in one process, so this is redone after
   * every navigation: the extension page lives in the extension process, not
   * the content process the tab started in, and a reload replaces the window.
   * `stale`, when given, names a mark left on the old document, so a target
   * that still answers from before the reload is not mistaken for the new one.
   */
  async attach({ timeout = 30_000, stale = null } = {}) {
    let seen = "(tab not listed)";
    await settle(async () => {
      const { tabs } = await this.client.request("root", "listTabs");
      // By actor, which listTabs keeps stable for a tab; by address as well,
      // so a descriptor that was replaced anyway is picked up rather than
      // reported missing.
      const tab = tabs.find((t) => t.actor === this.descriptor) ||
                  tabs.find((t) => t.url === this.url);
      if (!tab) { seen = "(tab gone)"; return false; }
      this.descriptor = tab.actor;
      seen = tab.url;
      if (tab.url !== this.url) return false;
      const { frame } = await this.client.request(this.descriptor, "getTarget");
      seen = `target at ${frame.url}`;
      if (frame.url !== this.url || !frame.consoleActor) return false;
      const state = await evaluateText(this.client, frame.consoleActor,
        `({ ready: document.readyState, href: location.href,
            stale: ${stale ? `!!window[${JSON.stringify(stale)}]` : "false"} })`);
      seen = `${state.href}, readyState ${state.ready}${state.stale ? ", not reloaded yet" : ""}`;
      if (state.href !== this.url || state.ready !== "complete" || state.stale) return false;
      this.consoleActor = frame.consoleActor;
      return true;
    }, { timeout, what: () => `${this.url} loading in the tab (saw ${seen})` });
    return this;
  }

  /**
   * Run fn(...args) in the page, like Playwright's page.evaluate.
   *
   * A promise is kept on the page and polled for, rather than awaited by the
   * console: whether the console waits on a promise depends on how the
   * evaluation was mapped, and a slot that is read back does not.
   */
  async evaluate(fn, ...args) {
    if (!this.consoleActor) throw new Error("not attached to a page");
    const slot = `__abshRdp${++nextSlot}`;
    const first = await evaluateText(this.client, this.consoleActor, `(() => {
      const value = (${fn})(...${JSON.stringify(args)});
      if (!value || typeof value.then !== "function") return { done: true, value };
      window[${JSON.stringify(slot)}] = { done: false };
      value.then(
        (v) => { window[${JSON.stringify(slot)}] = { done: true, value: v }; },
        (e) => { window[${JSON.stringify(slot)}] = { done: true, error: String((e && e.message) || e) }; });
      return { done: false };
    })()`);
    const out = first.done ? first : await settle(async () => {
      const s = await evaluateText(this.client, this.consoleActor,
        `window[${JSON.stringify(slot)}] || { lost: true }`);
      if (s.lost) throw new Error("the page navigated away before the promise settled");
      return s.done ? s : null;
    }, { timeout: REQUEST_TIMEOUT, what: `a promise from ${String(fn).slice(0, 60)} settling` });
    if (out.error) throw new Error(`page rejected: ${out.error}`);
    return out.value;
  }

  /** Poll fn(...args) in the page until it is truthy. */
  async waitFor(what, fn, args = [], { timeout = 15_000 } = {}) {
    return settle(() => this.evaluate(fn, ...args), { timeout, what });
  }

  /** Set a field's value the way typing does, so input listeners run. */
  fill(selector, value) {
    return this.evaluate((sel, v) => {
      const el = document.querySelector(sel);
      if (!el) throw new Error(`no ${sel}`);
      el.focus();
      el.value = v;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return el.value;
    }, selector, value);
  }

  /** Click, refusing a disabled control the way a user's click would do nothing. */
  click(selector) {
    return this.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) throw new Error(`no ${sel}`);
      if (el.disabled) throw new Error(`${sel} is disabled`);
      el.click();
      return true;
    }, selector);
  }

  /** Reload, and do not return until the replacement document is the one bound. */
  async reload({ timeout = 30_000 } = {}) {
    const mark = `__abshBeforeReload${++nextSlot}`;
    await this.evaluate((m) => { window[m] = true; return true; }, mark);
    // From the tab, as the reload button does, rather than location.reload()
    // in the page: a console reports its result after the evaluation, and by
    // then the document that would have been evaluated in is going away.
    await this.client.request(this.descriptor, "reloadDescriptor", { bypassCache: false });
    this.consoleActor = null;
    return this.attach({ timeout, stale: mark });
  }
}

/**
 * Move the tab whose URL contains `marker` to `url`, and bind to it there.
 *
 * Navigation goes through the tab descriptor, which loads with the system
 * principal the way the address bar does. A content page cannot navigate to
 * an extension page that is not web-accessible, and options.html is not.
 */
export async function openExtensionPage(client, { marker, url, timeout = 30_000 }) {
  const tab = await settle(async () => {
    const { tabs } = await client.request("root", "listTabs");
    return tabs.find((t) => (t.url || "").includes(marker)) || null;
  }, { timeout, what: `a tab showing ${marker}` });

  try {
    // waitForLoad: false still waits for the load to start, which may mean
    // starting the extension process first; allow for that.
    await client.request(tab.actor, "navigateTo", { url, waitForLoad: false }, timeout);
  } catch (e) {
    // Descriptors learned to navigate in later Firefoxes; before that it was
    // the target's request.
    if (e.error !== "unrecognizedPacketType") throw e;
    const { frame } = await client.request(tab.actor, "getTarget");
    await client.request(frame.actor, "navigateTo", { url });
  }
  return new ExtensionPage(client, tab.actor, url).attach({ timeout });
}
