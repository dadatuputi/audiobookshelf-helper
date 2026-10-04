/* Options page.
 *
 * Also where the host permission is granted. The extension ships with no host
 * permissions at all - it asks for the one origin the user configures, and a
 * permissions.request() has to come from a real user gesture, which is what
 * the Grant access button is. */
// The player (devicePath, subdir) is chosen in the popup, not here.
const FIELDS = ["absUrl", "apiKey", "folderTemplate"];
const CHECKS = ["renameM4b"];

const $ = (id) => document.getElementById(id);

/* -------------------------------------------------------------- the rows
 *
 * One row per thing that has to work, each with a dot and a line saying
 * whether it does. A row opens by itself when it starts needing you; after
 * that, opening and closing it is yours. */
const ROWS = {};                       // name -> { state, counted }
const BAD = new Set(["err", "warn"]);

function openRow(name, open) {
  const sec = $("sec-" + name);
  if (!sec) return;
  sec.classList.toggle("open", open);
  sec.querySelector(".sec-head").setAttribute("aria-expanded", String(open));
  sec.querySelector(".sec-body").hidden = !open;
}

/** state: ok | warn | err | off. An "off" row, or one not counted, is not
 *  part of what has to be ready. */
function setRow(name, state, summary, { counted = true } = {}) {
  const sec = $("sec-" + name);
  if (!sec) return;
  const was = sec.dataset.state;
  sec.dataset.state = state;
  $("sum-" + name).textContent = summary;
  ROWS[name] = { state, counted: counted && state !== "off" };
  if (BAD.has(state) && !BAD.has(was)) openRow(name, true);
  tally();
}

function tally() {
  const rows = Object.values(ROWS).filter((r) => r.counted);
  const ok = rows.filter((r) => r.state === "ok" || r.state === "warn").length;
  const out = $("ready");
  out.replaceChildren(Object.assign(document.createElement("b"),
                                    { textContent: `${ok} of ${rows.length}` }), " ready");
  out.classList.toggle("short", ok < rows.length);
}

document.addEventListener("click", (e) => {
  const head = e.target.closest(".sec-head");
  if (!head) return;
  const name = head.closest(".sec").id.replace(/^sec-/, "");
  openRow(name, head.getAttribute("aria-expanded") !== "true");
});

// Other scripts on this page (the Chrome folder section) add rows of their own.
globalThis.ABSH_SETTINGS = { setRow, openRow };

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

async function serverRow() {
  const s = await browser.storage.local.get({ absUrl: "", apiKey: "" });
  const host = hostOf(s.absUrl);
  if (!s.absUrl) setRow("server", "err", "Not set");
  else if (!host) setRow("server", "err", "The URL isn't a full address");
  else if (!s.apiKey) setRow("server", "err", `${host} · no API key`);
  else setRow("server", "ok", `${host} · key saved`);
}

async function namingRow() {
  const s = await browser.storage.local.get({ folderTemplate: ABSH.DEFAULTS.folderTemplate,
                                              renameM4b: true });
  setRow("naming", "ok", `${s.folderTemplate} · ` +
                         (s.renameM4b ? ".m4b → .m4a" : "keeps .m4b"));
}

function note(el, msg, cls) {
  el.textContent = msg;
  el.className = "note " + (cls || "");
}

function originOf(url) {
  try {
    return ABSH.originPattern(url);
  } catch {
    return null;
  }
}

async function refreshPermissionState() {
  const url = $("absUrl").value.trim();
  const pattern = originOf(url);
  const btn = $("grant");
  const out = $("permState");

  if (!pattern) {
    btn.disabled = true;
    note(out, url ? "Enter a full URL, e.g. http://media.local:13378" : "Set the server URL first.",
         url ? "err" : "");
    setRow("access", "err", "Set the server first");
    return false;
  }
  const granted = await browser.permissions.contains({ origins: [pattern] });
  btn.disabled = granted;
  note(out, granted ? `Access granted for ${pattern}` : `Not granted yet for ${pattern}`,
       granted ? "ok" : "warn");

  // A granted permission is not the same as a registered content script, and
  // the difference is invisible on the page: the in-page UI is simply absent.
  // Say which pages the script is actually registered for, and say so loudly
  // when registering failed.
  const reg = $("regState");
  const st = await browser.storage.local.get(["registrationError", "registeredPattern"]);
  if (!granted) setRow("access", "err", `Not granted · ${pattern}`);
  else if (st.registrationError) setRow("access", "err", "Granted · library page button missing");
  else setRow("access", "ok", `Granted · ${pattern}`);
  if (reg) {
    if (st.registrationError) {
      note(reg, `In-page UI not registered - ${st.registrationError}`, "err");
    } else if (st.registeredPattern) {
      note(reg, `In-page UI active on ${st.registeredPattern}`, "ok");
    } else if (granted) {
      note(reg, "In-page UI not registered yet. Save, then reload the library page.", "warn");
    } else {
      note(reg, "", "");
    }
  }
  return granted;
}

async function load() {
  const d = await browser.storage.local.get(ABSH.DEFAULTS);
  // Only fill what is still untouched. Reading storage is asynchronous, and
  // anything typed while it was in flight used to be overwritten the moment
  // it resolved - the first characters of a pasted server URL simply
  // vanished, and Save then stored the empty field.
  //
  // Untouched, not empty: folderTemplate carries its default in the markup,
  // so testing for an empty field never showed a saved value for it. The page
  // reverted to the default on every load, and the next Save quietly wrote
  // the default back over the user's choice.
  for (const k of FIELDS) if ($(k) && !$(k).dataset.touched) $(k).value = d[k] || "";
  for (const k of CHECKS) if (!$(k).dataset.touched) $(k).checked = !!d[k];
  await Promise.all([refreshPermissionState(), serverRow(), namingRow()]);
}

/** Put the fields back to what is saved. */
async function revert() {
  for (const k of [...FIELDS, ...CHECKS]) delete $(k).dataset.touched;
  await load();
}

$("grant").addEventListener("click", async () => {
  const pattern = originOf($("absUrl").value.trim());
  if (!pattern) return;
  try {
    const ok = await browser.permissions.request({ origins: [pattern] });
    if (!ok) {
      note($("permState"), "Permission was declined.", "err");
      return;
    }
    // The toolbar button is registered against this origin, so tell the
    // background to (re)register now that the grant exists.
    await browser.runtime.sendMessage({ type: "permissionChanged" });
    await refreshPermissionState();
  } catch (e) {
    note($("permState"), String(e.message || e), "err");
  }
});

$("absUrl").addEventListener("input", refreshPermissionState);

// Typed into since the page filled it: a late read from storage leaves it be.
for (const k of FIELDS) {
  if ($(k)) $(k).addEventListener("input", () => { $(k).dataset.touched = "1"; });
}
for (const k of CHECKS) {
  if ($(k)) $(k).addEventListener("change", () => { $(k).dataset.touched = "1"; });
}

/* ------------------------------------------------------------ the helper
 *
 * Which helper is installed, whether a newer one is out, and a button to
 * install it - but only when this copy can actually replace itself. When it
 * cannot (a git checkout, a folder you cannot write to), the helper's own
 * sentence says why, and there is no button that would only fail. */

let UPDATING = false;
let FOLDER_IN_USE = false;             // Chrome is writing to a folder itself
let LAST_HELPER = null;

function helperRow(s) {
  LAST_HELPER = s;
  if (s.kind === "unreachable") {
    if (FOLDER_IN_USE) setRow("helper", "off", "Not used · Chrome writes to the folder");
    else setRow("helper", "err", "Not responding");
    return;
  }
  const v = s.installed;
  switch (s.kind) {
    case "available": setRow("helper", "warn", `${v} · ${s.latest} available`); break;
    case "current": setRow("helper", "ok", `${v} · Up to date`); break;
    case "unversioned": setRow("helper", "ok", `${v} · can't update itself`); break;
    case "unsupported": setRow("helper", "warn", `${v} · too old to update from here`); break;
    default: setRow("helper", "ok", v);
  }
}

/** Without the helper, in Chrome, its row is not something to fix. */
globalThis.ABSH_SETTINGS.helperUnused = (unused) => {
  FOLDER_IN_USE = unused;
  if (LAST_HELPER) helperRow(LAST_HELPER);
};

function showHelper({ helper, check }, checking) {
  const s = ABSH.updateState(helper, check);
  const version = $("helperVersion");
  const state = $("updateState");
  const btn = $("update");
  const checkBtn = $("checkUpdate");
  const when = $("checkedAt");

  btn.classList.add("hidden");
  checkBtn.classList.toggle("hidden", s.kind === "unreachable");

  if (s.kind === "unreachable") {
    version.textContent = "The helper isn't responding.";
    note(state, `${(s.error || "No answer").replace(/\.$/, "")}. Run install.py ` +
                "from the download (native/install.py in a checkout), then restart " +
                "the browser.", "err");
    when.textContent = "";
    helperRow(s);
    return;
  }

  version.textContent = `Helper version ${s.installed}`;
  const latest = s.latest ? `${s.latest}${s.prerelease ? " (prerelease)" : ""}` : "";

  switch (s.kind) {
    case "available":
      if (s.canUpdate) {
        note(state, `${latest} is available.`, "warn");
        btn.textContent = `Update to ${s.latest}`;
        btn.dataset.tag = s.latest;
        btn.classList.remove("hidden");
      } else if (s.refused) {
        // This copy cannot replace itself at all: a checkout, a folder it
        // cannot write, or - in every build until a key is pinned - no key
        // to tell a genuine release from a forged one.
        note(state, `${latest} is available, but it can't be installed from here: ` +
                    s.refused, "warn");
      } else {
        // This copy could, but not this release: unsigned, or not by a key
        // it trusts.
        note(state, `${latest} is available, but the helper won't install it: ` +
                    s.releaseRefused, "warn");
      }
      break;
    case "current":
      note(state, (s.ahead
        ? `Up to date - newer than the latest release, ${latest}.`
        : "Up to date.") +
        (s.refused ? ` Later releases can't be installed from here, though: ${s.refused}` : ""),
        "ok");
      break;
    case "unversioned":
      note(state, (s.refused ? `This copy can't update itself: ${s.refused}` :
                               "This copy has no release version to update from.") +
                  (latest ? ` The latest release is ${latest}.` : ""), "");
      break;
    case "unsupported":
      note(state, "This helper is too old to be updated from here. Download the " +
                  "latest release and run its install.py once; later updates can " +
                  "then be installed from this page.", "warn");
      break;
    case "failed":
      note(state, "Couldn't check for updates.", "");
      break;
    default:
      note(state, checking ? "" : "Not checked for updates yet.", "");
  }

  helperRow(s);

  if (checking) {
    when.textContent = "Checking for updates…";
  } else if (s.error) {
    when.textContent = `Last check failed: ${s.error}`;
  } else if (s.checkedAt) {
    when.textContent = `Checked ${new Date(s.checkedAt).toLocaleString()}.`;
  } else {
    when.textContent = "";
  }
}

async function helperStatus(opts) {
  const r = await browser.runtime.sendMessage({ type: "updateStatus", ...opts });
  if (!r || !r.ok) throw new Error((r && r.error) || "no response");
  return r.data;
}

/** Show what is known at once, then ask the release feed if that is due -
 *  the version should not wait on GitHub. Resolves to the final status. */
async function refreshHelper(force) {
  if (UPDATING) return null;
  const checkBtn = $("checkUpdate");
  checkBtn.disabled = true;
  try {
    let st = await helperStatus({ force, peek: true });
    showHelper(st, st.due);
    if (st.due) {
      st = await helperStatus({ force });
      showHelper(st, false);
    }
    return st;
  } catch (e) {
    showHelper({ helper: { ok: false, error: String(e.message || e) }, check: null });
    return null;
  } finally {
    checkBtn.disabled = false;
  }
}

/* Installing streams its steps, so it goes over a port rather than a single
 * message - the same way the popup streams a sync. */
function runUpdate(tag, onStep) {
  return new Promise((resolve, reject) => {
    const p = browser.runtime.connect({ name: "absh" });
    p.onMessage.addListener((m) => {
      if (m.progress) {
        if (m.progress.event === "step") onStep(m.progress.message);
        return;
      }
      p.disconnect();
      m.ok ? resolve(m.data) : reject(new Error(m.error || "no response"));
    });
    p.onDisconnect.addListener(() => reject(new Error("the extension's background stopped")));
    p.postMessage({ type: "update", tag, rid: 1 });
  });
}

$("update").addEventListener("click", async () => {
  const btn = $("update");
  const steps = $("updateSteps");
  const tag = btn.dataset.tag;
  UPDATING = true;
  btn.disabled = true;
  $("checkUpdate").disabled = true;
  btn.textContent = "Updating…";
  steps.innerHTML = "";
  note($("updateState"), `Installing ${tag}. Keep this page open until it finishes.`, "");
  try {
    const r = await runUpdate(tag, (msg) => {
      const li = document.createElement("li");
      li.textContent = msg;
      steps.appendChild(li);
    });
    UPDATING = false;
    // Asking again reaches a fresh helper started from the new files, so the
    // version it reports is the one now installed - which is the proof the
    // swap took, rather than the old process's word for it.
    const st = await refreshHelper(false);
    const now = st && st.helper && st.helper.ok ? st.helper.release : "";
    if (!r.updated) {
      note($("updateState"), "Already on the latest release.", "ok");
    } else if (ABSH.compareVersions(now, r.to) === 0) {
      note($("updateState"), `Updated to ${r.to}. The helper restarted on the new version.`, "ok");
    } else if (now) {
      note($("updateState"), `${r.to} is installed, but the helper still reports ` +
                             `${now}. Restart the browser to pick it up.`, "warn");
    }
  } catch (e) {
    UPDATING = false;
    btn.textContent = `Update to ${tag}`;
    // The helper's own words. They say what went wrong and whether the old
    // version was put back, and rewording them would lose that.
    note($("updateState"), `The update didn't finish: ${e.message || e}`, "err");
  } finally {
    UPDATING = false;
    btn.disabled = false;
    $("checkUpdate").disabled = false;
  }
});

$("checkUpdate").addEventListener("click", () => refreshHelper(true));

/* Saving says what it changed, in words, beside the button that did it. */
const LABELS = { absUrl: "URL", apiKey: "API key", folderTemplate: "folder template",
                 renameM4b: ".m4b renaming" };

function inWords(list) {
  return list.length < 2 ? list.join("")
    : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

async function save(msg) {
  const before = await browser.storage.local.get(ABSH.DEFAULTS);
  const o = {};
  for (const k of FIELDS) o[k] = $(k).value.trim();
  for (const k of CHECKS) o[k] = $(k).checked;
  const changed = Object.keys(o).filter((k) => (before[k] ?? "") !== o[k]);
  await browser.storage.local.set(o);
  for (const k of [...FIELDS, ...CHECKS]) delete $(k).dataset.touched;
  for (const m of document.querySelectorAll(".msg")) m.textContent = "";
  msg.textContent = changed.length
    ? `Saved ${inWords(changed.map((k) => LABELS[k]))}.` : "Nothing had changed.";
  clearTimeout(save.timer);
  save.timer = setTimeout(() => { msg.textContent = ""; }, 4000);
  await Promise.all([refreshPermissionState(), serverRow(), namingRow()]);
}

$("save").addEventListener("click", () => save($("msg")));
$("saveNaming").addEventListener("click", () => save($("msgNaming")));
for (const b of document.querySelectorAll(".sec .cancel")) b.addEventListener("click", revert);

$("showKey").addEventListener("click", () => {
  const key = $("apiKey");
  const show = key.type === "password";
  key.type = show ? "text" : "password";
  $("showKey").textContent = show ? "Hide" : "Show";
});

/* Every action has a key, and the key is printed on it. */
const MAC = /Mac|iPhone|iPad/.test(navigator.userAgent);
for (const k of document.querySelectorAll(".kbd-save")) k.textContent = MAC ? "⌘S" : "Ctrl S";
document.addEventListener("keydown", (e) => {
  const sec = e.target.closest && e.target.closest(".sec");
  if (e.key.toLowerCase() === "s" && (MAC ? e.metaKey : e.ctrlKey) && !e.altKey) {
    e.preventDefault();
    save(sec && sec.id === "sec-naming" ? $("msgNaming") : $("msg"));
  } else if (e.key === "Escape" && sec && sec.querySelector(".cancel")) {
    e.preventDefault();
    revert();
  }
});

load();
refreshHelper(false);
