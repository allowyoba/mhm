/* mhm owns one persistent native connection. Source material never enters storage. */
if (typeof importScripts === "function")
  importScripts(
    "background/identity.js",
    "background/native.js",
    "background/proxy.js",
    "background/action.js",
  );
const HOST = MHM_NATIVE_HOST;
const toolbar = new MhmToolbar();
let operation = null;
let state = { status: "disconnected", profileId: null };
let profiles = [];
let selectedProfileId = null;
let preferences = { killSwitch: true, theme: "system" };
const native = new MhmNativeClient(HOST, () => {
  const active = ["connected", "connecting", "blocked", "limited"].includes(
    state.status,
  );
  if (active)
    protectStoppedConnection({
      status: "disconnected",
      stopReason: state.status === "limited" ? "traffic-limit" : null,
    }).catch(() => {});
  else {
    state = { status: "disconnected", profileId: null };
    broadcast();
  }
});
const ready = chrome.storage.local
  .get(["mhmPreferences", "selectedProfileId", "mhmConnection"])
  .then(async (stored) => {
    preferences = { ...preferences, ...stored.mhmPreferences };
    selectedProfileId = stored.selectedProfileId || null;
    if (stored.mhmConnection?.active) {
      state = {
        status:
          stored.mhmConnection.status === "limited"
            ? "limited"
            : preferences.killSwitch
              ? "blocked"
              : "disconnected",
        profileId: null,
      };
      await setBrowserProxy(
        state.status === "limited" || preferences.killSwitch ? 9 : null,
      );
    }
    toolbar.update(state, profiles);
  });

function broadcast() {
  toolbar.update(state, profiles);
  chrome.storage.local
    .set({
      mhmConnection: {
        active: ["connected", "connecting", "blocked", "limited"].includes(
          state.status,
        ),
        status: state.status,
      },
    })
    .catch(() => {});
  chrome.runtime
    .sendMessage({
      type: "stateChanged",
      state,
      profiles,
      selectedProfileId,
      operation,
    })
    .catch(() => {});
}
function nativeRequest(command, payload = {}, timeout = 90000) {
  return native.request(command, payload, timeout);
}

async function protectStoppedConnection(result) {
  const limited = result.stopReason === "traffic-limit";
  await setBrowserProxy(limited || preferences.killSwitch ? 9 : null);
  state = {
    ...result,
    status: limited
      ? "limited"
      : preferences.killSwitch
        ? "blocked"
        : "disconnected",
  };
  broadcast();
}

async function refresh() {
  const result = await nativeRequest("bootstrap");
  profiles = result.profiles || [];
  const protectedDisconnect =
    ["blocked", "limited"].includes(state.status) &&
    result.status.status === "disconnected";
  state = protectedDisconnect
    ? { ...result.status, status: state.status }
    : result.status;
  result.status = state;
  if (!profiles.some((p) => p.id === selectedProfileId))
    selectedProfileId = profiles[0]?.id || null;
  await chrome.storage.local.set({ selectedProfileId });
  // Recover an interrupted service worker while the native port is still alive.
  if (state.status === "connected") await setBrowserProxy(state.localPort);
  broadcast();
  return result;
}
async function connect(profileId) {
  state = { ...state, status: "connecting" };
  broadcast();
  try {
    if (preferences.killSwitch) await setBrowserProxy(9);
    const result = await nativeRequest("connect", { profileId });
    await setBrowserProxy(result.localPort);
    state = result;
    selectedProfileId = profileId;
    await chrome.storage.local.set({ selectedProfileId });
    return result;
  } catch (error) {
    if (error.code === "TRAFFIC_LIMIT") {
      await setBrowserProxy(9);
      state = { status: "limited", profileId: null };
      throw error;
    }
    // A failed candidate can leave the prior core running after native rollback.
    const current = await nativeRequest("status", {}, 5000).catch(() => null);
    if (current?.status === "connected") {
      await setBrowserProxy(current.localPort);
      state = current;
    } else {
      await setBrowserProxy(preferences.killSwitch ? 9 : null);
      state = {
        status: preferences.killSwitch ? "blocked" : "disconnected",
        profileId: null,
      };
    }
    throw error;
  } finally {
    broadcast();
  }
}
async function disconnect() {
  await nativeRequest("disconnect", {}, 15000);
  await setBrowserProxy(null);
  state = { status: "disconnected", profileId: null };
  broadcast();
}

const mutating = new Set([
  "saveMihomoProfile",
  "updateMihomoProfile",
  "rollbackMihomoProfile",
  "deleteProfile",
  "connect",
  "disconnect",
  "selectProxy",
  "setMode",
  "saveSettings",
  "updateDueProfiles",
  "saveTrafficPolicy",
  "saveProfileRules",
]);
const allowed = new Set([
  ...mutating,
  "groups",
  "proxyDelay",
  "profileSource",
  "profileContent",
  "trafficStats",
  "getSettings",
  "trafficOverview",
  "profileRules",
]);
async function dispatch(message) {
  await ready;
  const { action, ...payload } = message;
  if (action === "initialize") {
    const result = await refresh();
    return { ...result, preferences, selectedProfileId, operation };
  }
  if (action === "ping") return nativeRequest("ping", {}, 10000);
  if (action === "selectProfile") {
    selectedProfileId = payload.profileId;
    await chrome.storage.local.set({ selectedProfileId });
    broadcast();
    return {};
  }
  if (action === "preferences") {
    preferences = { ...preferences, ...payload.preferences };
    if (state.status === "blocked" && !preferences.killSwitch) {
      await setBrowserProxy(null);
      state = { status: "disconnected", profileId: null };
      broadcast();
    }
    await chrome.storage.local.set({ mhmPreferences: preferences });
    return { preferences };
  }
  if (!allowed.has(action)) throw new Error("Unknown action");
  if (mutating.has(action) && operation && action !== "disconnect")
    throw new Error("Wait for the current operation to finish.");
  const ownsOperation = mutating.has(action) && !operation;
  if (ownsOperation) {
    operation = action;
    broadcast();
  }
  try {
    if (action === "connect") return await connect(payload.profileId);
    if (action === "disconnect") {
      await disconnect();
      return {};
    }
    const response = await nativeRequest(action, payload);
    if (response.profiles) profiles = response.profiles;
    if (action === "setMode") state = response;
    const budget = response.budget || response.traffic?.budget;
    if (budget && state.status === "limited" && !budget.blocked) {
      // Keep traffic protected until the user reconnects or disconnects.
      state = { status: "blocked", profileId: null };
      broadcast();
    }
    if (budget) toolbar.update(state, profiles, budget);
    if (budget?.blocked && state.status === "connected") {
      await nativeRequest("disconnect");
      await protectStoppedConnection({
        status: "disconnected",
        stopReason: "traffic-limit",
      });
    }
    if (
      action === "deleteProfile" &&
      !profiles.some((p) => p.id === selectedProfileId)
    ) {
      selectedProfileId = profiles[0]?.id || null;
      await chrome.storage.local.set({ selectedProfileId });
    }
    return response;
  } finally {
    if (ownsOperation) {
      operation = null;
      broadcast();
    }
  }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message?.action) return false;
  dispatch(message)
    .then((data) => sendResponse({ ...data, ok: true }))
    .catch((error) =>
      sendResponse({
        ok: false,
        error: error.message,
        errorCode: error.code || "INTERNAL_ERROR",
      }),
    );
  return true;
});
chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  await chrome.alarms.create("profile-updates", { periodInMinutes: 30 });
  if (reason === "install")
    await chrome.tabs.create({ url: chrome.runtime.getURL("setup.html") });
  // Old UI caches contain URLs and node metadata. Native encrypted originals stay available.
  await chrome.storage.local.remove([
    "profilesCache",
    "subscriptionsCache",
    "profilesVersion",
    "subscriptionsVersion",
  ]);
});
chrome.alarms.onAlarm.addListener(async (alarm) => {
  await ready;
  if (alarm.name === "profile-updates" && !operation) {
    await dispatch({ action: "updateDueProfiles" }).catch(() => {});
  }
  if (
    alarm.name === "connection-health" &&
    state.status === "connected" &&
    !operation
  ) {
    try {
      const result = await nativeRequest("status", {}, 5000);
      if (result.status !== "connected") await protectStoppedConnection(result);
      else toolbar.update(state, profiles, result.traffic?.budget);
    } catch {
      /* Native disconnect handles proxy protection. */
    }
  }
});
chrome.alarms.create("connection-health", { periodInMinutes: 0.5 });

chrome.alarms.create("profile-updates", { periodInMinutes: 30 });
