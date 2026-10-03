/* Options page.
 *
 * Also where the host permission is granted. The extension ships with no host
 * permissions at all - it asks for the one origin the user configures, and a
 * permissions.request() has to come from a real user gesture, which is what
 * the Grant access button is. */
const FIELDS = ["absUrl", "apiKey", "devicePath", "subdir", "folderTemplate"];
const CHECKS = ["renameM4b"];

const $ = (id) => document.getElementById(id);

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
  if (reg) {
    const st = await browser.storage.local.get(["registrationError", "registeredPattern"]);
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
  // Untouched, not empty: subdir and folderTemplate carry their defaults in
  // the markup, so testing for an empty field never showed a saved value for
  // them. The page reverted to the default on every load, and the next Save
  // quietly wrote the default back over the user's choice.
  for (const k of FIELDS) if ($(k) && !$(k).dataset.touched) $(k).value = d[k] || "";
  for (const k of CHECKS) if (!$(k).dataset.touched) $(k).checked = !!d[k];
  await refreshPermissionState();
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

/* The player's path is the one setting nobody can type from memory, so ask
   the helper what is actually plugged in. */
$("detect").addEventListener("click", async () => {
  const list = $("deviceList");
  const btn = $("detect");
  btn.disabled = true;
  btn.textContent = "Looking…";
  try {
    const found = await browser.runtime.sendMessage({ type: "devices" });
    if (!found || !found.ok) throw new Error((found && found.error) || "no response");
    const devices = found.data || [];
    list.innerHTML = "";
    if (!devices.length) {
      note($("permState"), "Nothing removable is mounted. Some players need " +
                           "USB Mode → MSC before they appear as a drive.", "warn");
      list.classList.add("hidden");
      return;
    }
    for (const d of devices) {
      const o = document.createElement("option");
      o.value = d.path;
      const free = d.free ? ` — ${Math.round(d.free / 1073741824)}GB free` : "";
      const has = d.hasSubdir ? "  ✓ has your books folder" : "";
      o.textContent = `${d.name}${free}${has}`;
      list.appendChild(o);
    }
    list.classList.remove("hidden");
    list.size = Math.min(6, devices.length + 1);
    // The most player-like one is first; offer it straight away.
    list.selectedIndex = 0;
    $("devicePath").value = devices[0].path;
  } catch (e) {
    note($("permState"), "Could not reach the helper: " + (e.message || e), "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Detect";
  }
});

$("deviceList").addEventListener("change", () => {
  $("devicePath").value = $("deviceList").value;
});

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

function showHelper({ helper, check }, checking) {
  const s = ABSH.updateState(helper, check);
  const version = $("helperVersion");
  const state = $("updateState");
  const btn = $("update");
  const checkBtn = $("checkUpdate");
  const when = $("checkedAt");

  btn.classList.add("hidden");
  checkBtn.classList.toggle("hidden", s.kind === "unreachable");
  $("pollNote").classList.toggle("hidden", !(helper && helper.polls === true));

  if (s.kind === "unreachable") {
    version.textContent = "The helper isn't responding.";
    note(state, `${(s.error || "No answer").replace(/\.$/, "")}. Run install.py ` +
                "from the download (native/install.py in a checkout), then restart " +
                "the browser.", "err");
    when.textContent = "";
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

$("save").addEventListener("click", async () => {
  const o = {};
  for (const k of FIELDS) o[k] = $(k).value.trim();
  for (const k of CHECKS) o[k] = $(k).checked;
  await browser.storage.local.set(o);
  const m = $("msg");
  m.textContent = "saved";
  setTimeout(() => { m.textContent = ""; }, 1500);
  await refreshPermissionState();
});

load();
refreshHelper(false);
