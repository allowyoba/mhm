import { VERSION } from "./lib/version.js";
import { FIREFOX_ID } from "./lib/identity.js";
import { $, send } from "./lib/api.js";
import { formatBytes, formatRate } from "./lib/format.js";
import { createTrafficView } from "./lib/traffic-view.js";
import { createRulesView } from "./lib/rules-view.js";
const t = (key) => AppI18n.t(key);
let profiles = [];
let state = { status: "disconnected" };
let selectedProfileId = null;
let editingId = null;
let preferences = {};
let busy = false;
let localBusy = false;
let remoteOperation = null;
let dirty = false;
let currentView = "loadingView";
let pollTimer = null;
const groupDelays = new Map();
let groupDelayScope = null;

const trafficView = createTrafficView({ t, run });
const rulesView = createRulesView({
  t,
  run,
  confirmDiscard: () =>
    confirmAction(t("discardTitle"), t("discardHint"), t("discard")),
  getState: () => state,
  onProfilesChanged: (next) => {
    profiles = next;
    render();
  },
  onApply: async (profileId) => {
    state = await send("connect", { profileId });
    selectedProfileId = profileId;
    render();
    await refreshGroups();
  },
});

const isPopup = new URLSearchParams(location.search).get("popup") === "1";
if (isPopup) document.body.classList.add("popup");
function translate() {
  document.documentElement.lang = AppI18n.locale.replace("_", "-");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
}
function notice(message = "", error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("hidden", !message);
  $("notice").classList.toggle("error", error);
}
function navigate(view) {
  document
    .querySelectorAll(".view")
    .forEach((node) => node.classList.toggle("hidden", node.id !== view));
  currentView = view;
  $("appNavigation").classList.toggle(
    "hidden",
    ["loadingView", "onboardingView"].includes(view),
  );
  document.querySelectorAll("[data-route]").forEach((button) => {
    const active = button.dataset.route === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (view === "trafficView")
    trafficView.refresh().catch((error) => notice(error.message, true));
  if (view === "rulesView")
    rulesView
      .refresh(profiles, selectedProfileId)
      .catch((error) => notice(error.message, true));
  window.scrollTo({ top: 0 });
}
function applyTheme(value) {
  if (value === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = value;
}
async function run(work, success = "") {
  if (busy) return;
  localBusy = true;
  busy = true;
  $("editorFields").disabled = true;
  render();
  notice(t("working"));
  try {
    await work();
    notice(success);
  } catch (error) {
    notice(error.message, true);
  } finally {
    localBusy = false;
    busy = Boolean(remoteOperation);
    $("editorFields").disabled = false;
    render();
  }
}
const byteRate = formatRate;
function profileSubtitle(profile) {
  const parts = [
    t(profile.source === "subscription" ? "subscription" : "localYaml"),
  ];
  if (profile.transport === "wireguard") parts.push("WireGuard");
  if (profile.needsDownload) parts.push(t("downloadNeeded"));
  if (profile.tlsMode === "insecure") parts.push(t("tlsInsecureBadge"));
  if (profile.lastUpdatedAt)
    parts.push(
      new Date(profile.lastUpdatedAt * 1000).toLocaleDateString(
        AppI18n.locale.replace("_", "-"),
      ),
    );
  if (profile.lastError) parts.push(t("updateFailed"));
  if (state.profileId === profile.id && state.revision !== profile.revision)
    parts.push(t("updateReady"));
  if (profile.usage?.total) {
    const used = (profile.usage.upload || 0) + (profile.usage.download || 0);
    parts.push(
      `${(used / 1073741824).toFixed(1)} / ${(profile.usage.total / 1073741824).toFixed(1)} GB`,
    );
  }
  if (profile.usage?.expire)
    parts.push(
      `${t("expires")} ${new Date(profile.usage.expire * 1000).toLocaleDateString(AppI18n.locale.replace("_", "-"))}`,
    );
  return parts.join(" · ");
}
function render() {
  const selected = profiles.find((p) => p.id === selectedProfileId);
  const connected = state.status === "connected";
  const connecting = state.status === "connecting";
  const limited = state.status === "limited";
  const blocked = state.status === "blocked" || limited;
  $("statusText").textContent = t(
    connected
      ? "connected"
      : connecting
        ? "connecting"
        : limited
          ? "limitReached"
          : blocked
            ? "blocked"
            : "disconnected",
  );
  $("connectionState").className = `connection-state ${state.status}`;
  $("activeName").textContent = selected?.name || t("chooseProfile");
  const active = profiles.find((p) => p.id === state.profileId);
  $("connectionHint").textContent = limited
    ? t("limitBlockedHint")
    : blocked
      ? t("blockedHint")
      : connected && state.profileId !== selectedProfileId
        ? `${t("activeProfile")} ${active?.name || ""}`
        : connected && state.revision !== selected?.revision
          ? t("applyUpdateHint")
          : connected
            ? t("connectedHint")
            : t("connectHint");
  const same = connected && state.profileId === selectedProfileId;
  $("connectButton").textContent = t(
    blocked
      ? selected
        ? "connect"
        : "disconnect"
      : same && state.revision !== selected?.revision
        ? "applyUpdate"
        : same
          ? "disconnect"
          : connected
            ? "switchProfile"
            : connecting
              ? "connecting"
              : "connect",
  );
  if (selected?.needsDownload)
    $("connectButton").textContent = t("downloadProfile");
  if (limited && !selected?.needsDownload)
    $("connectButton").textContent = t("manageLimit");
  $("connectButton").disabled =
    busy || connecting || (!selected && !blocked && !connected);
  const query = $("profileSearch").value.trim().toLowerCase();
  $("homeProfile").replaceChildren();
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    $("homeProfile").append(option);
  }
  $("homeProfile").value = selectedProfileId || "";
  $("homeProfile").disabled = busy || profiles.length === 0;
  $("profileList").replaceChildren();
  for (const profile of profiles.filter((item) =>
    item.name.toLowerCase().includes(query),
  )) {
    const row = document.createElement("div");
    row.className = `profile-row${profile.id === selectedProfileId ? " selected" : ""}`;
    const dot = document.createElement("span");
    dot.className = "profile-dot";
    const select = document.createElement("button");
    select.className = "profile-select";
    const name = document.createElement("strong");
    name.textContent = profile.name;
    const description = document.createElement("small");
    description.textContent = profileSubtitle(profile);
    select.append(name, description);
    select.setAttribute(
      "aria-pressed",
      String(profile.id === selectedProfileId),
    );
    select.disabled = busy;
    select.addEventListener("click", () =>
      run(async () => {
        await send("selectProfile", { profileId: profile.id });
        selectedProfileId = profile.id;
      }),
    );
    const edit = document.createElement("button");
    edit.className = "profile-edit";
    edit.textContent = "···";
    edit.setAttribute("aria-label", `${t("editProfile")} ${profile.name}`);
    edit.disabled = busy;
    edit.addEventListener("click", () => run(() => openEditor(profile)));
    row.append(dot, select, edit);
    $("profileList").append(row);
  }
  $("emptyProfiles").classList.toggle("hidden", profiles.length > 0);
  $("profileSearchEmpty").classList.toggle(
    "hidden",
    !profiles.length || $("profileList").childElementCount > 0,
  );
  $("groupsPanel").classList.toggle("hidden", !connected);
  $("liveTraffic").classList.toggle("hidden", !connected);
  $("disconnectLink").classList.toggle(
    "hidden",
    !(
      blocked ||
      (connected && (busy || state.profileId !== selectedProfileId))
    ),
  );
  $("mode").value = state.mode || "rule";
  $("mode").disabled = busy;
  rulesView.sync(profiles, state);
  $("addProfile").disabled = busy;
  $("profileActions")
    .querySelectorAll("button")
    .forEach((button) => {
      button.disabled = busy;
    });
}
function renderGroups(groups) {
  const scope = JSON.stringify([state.profileId, state.revision]);
  if (scope !== groupDelayScope) {
    groupDelays.clear();
    groupDelayScope = scope;
  }
  $("groups").replaceChildren();
  for (const group of groups) {
    const row = document.createElement("div");
    row.className = "group-row";
    if (group.type === "Selector") {
      const id = `group-${$("groups").childElementCount}`;
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = group.name;
      const select = document.createElement("select");
      select.id = id;
      for (const choice of group.choices) {
        const option = document.createElement("option");
        option.value = choice.name;
        option.textContent =
          choice.name + (choice.delay > 0 ? ` · ${choice.delay} ms` : "");
        select.append(option);
      }
      select.value = group.now;
      select.addEventListener("change", () =>
        run(async () => {
          const response = await send("selectProxy", {
            group: group.name,
            proxy: select.value,
          });
          renderGroups(response.groups);
        }),
      );
      row.append(label, select);
    } else {
      const title = document.createElement("strong");
      title.textContent = group.name;
      const detail = document.createElement("div");
      detail.className = "group-info";
      const types = {
        URLTest: t("groupAuto"),
        LoadBalance: t("groupBalance"),
        Fallback: t("groupFallback"),
      };
      detail.textContent = `${group.now || t("automaticSelection")} · ${types[group.type] || group.type}`;
      row.append(title, detail);
    }
    const result = document.createElement("output");
    result.className = "group-delay muted small";
    result.setAttribute("aria-live", "polite");
    if (groupDelays.has(group.name))
      result.textContent = `${groupDelays.get(group.name)} ms`;
    const test = document.createElement("button");
    test.className = "text-button small";
    test.textContent = t("testDelay");
    test.disabled = !group.name;
    test.addEventListener("click", () =>
      run(async () => {
        test.disabled = true;
        test.textContent = t("testingDelay");
        try {
          // Every group has a name. `now` is optional for load balancing and
          // other automatic groups, and is not required by the delay API.
          const response = await send("proxyDelay", { proxy: group.name });
          if (
            typeof response.delay !== "number" ||
            !Number.isFinite(response.delay) ||
            response.delay < 0
          ) {
            throw new Error(t("delayUnavailable"));
          }
          groupDelays.set(group.name, response.delay);
          result.textContent = `${response.delay} ms`;
          await refreshGroups();
        } finally {
          test.disabled = !group.name;
          test.textContent = t("testDelay");
        }
      }),
    );
    row.append(test, result);
    $("groups").append(row);
  }
}
async function refreshGroups() {
  if (state.status !== "connected") return;
  const result = await send("groups");
  renderGroups(result.groups);
}
function clearSecrets() {
  for (const id of [
    "subscriptionUrl",
    "yamlContent",
    "wireguardConfig",
    "requestHeaders",
  ])
    $(id).value = "";
  $("subscriptionUrl").type = "password";
  $("showUrl").textContent = t("showUrl");
  $("caCertificate").value = "";
  $("certificateFile").value = "";
  $("wireguardFile").value = "";
  $("yamlFile").value = "";
}
function editorVisibility() {
  const subscription = $("sourceType").value === "subscription";
  $("subscriptionFields").classList.toggle("hidden", !subscription);
  $("yamlFields").classList.toggle("hidden", subscription);
  $("wireguardFields").classList.toggle(
    "hidden",
    !subscription || $("transport").value !== "wireguard",
  );
  $("certificateFields").classList.toggle(
    "hidden",
    !subscription || $("tlsMode").value !== "custom",
  );
  $("insecureWarning").classList.toggle(
    "hidden",
    !subscription || $("tlsMode").value !== "insecure",
  );
  $("caCertificate").required = subscription && $("tlsMode").value === "custom";
  $("subscriptionUrl").required = subscription;
  $("yamlContent").required = !subscription;
}
async function openEditor(profile = null) {
  clearSecrets();
  editingId = profile?.id || null;
  $("profileForm").reset();
  $("profileName").value = profile?.name || "";
  $("sourceType").value = profile?.source || "subscription";
  $("transport").value = profile?.transport || "direct";
  $("tlsMode").value = "system";
  $("caCertificate").value = "";
  $("certificateFile").value = "";
  $("updateInterval").value = String(profile?.updateIntervalMinutes ?? 1440);
  $("editorTitle").textContent = t(profile ? "editProfile" : "addProfile");
  $("profileActions").classList.toggle("hidden", !profile);
  $("updateProfile").classList.toggle(
    "hidden",
    profile?.source !== "subscription",
  );
  $("rollbackProfile").classList.toggle("hidden", !profile?.hasRollback);
  $("wireguardConfig").placeholder = "";
  if (profile) {
    const result = await send("profileSource", { profileId: profile.id });
    $("subscriptionUrl").value = result.url;
    $("tlsMode").value = result.tlsMode || "system";
    $("caCertificate").value = result.caCertificate || "";
    if (result.source === "local" || !profile.source) {
      let offset = 0;
      let content = "";
      do {
        const part = await send("profileContent", {
          profileId: profile.id,
          offset,
        });
        content += part.text;
        offset = part.nextOffset;
      } while (offset !== null);
      $("yamlContent").value = content;
    }
    $("requestHeaders").value = Object.keys(result.headers).length
      ? JSON.stringify(result.headers, null, 2)
      : "";
    if (result.hasWireguard)
      $("wireguardConfig").placeholder = t("wireguardSaved");
  }
  editorVisibility();
  dirty = false;
  navigate("editorView");
  $("profileName").focus();
}
function confirmAction(title, text, okLabel = t("delete")) {
  $("confirmTitle").textContent = title;
  $("confirmText").textContent = text;
  $("confirmOk").textContent = okLabel;
  const dialog = $("confirmDialog");
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "ok"),
      { once: true },
    );
  });
}
async function leaveEditor() {
  if (busy) return;
  if (
    dirty &&
    !(await confirmAction(t("discardTitle"), t("discardHint"), t("discard")))
  )
    return;
  clearSecrets();
  dirty = false;
  navigate("homeView");
  notice();
}
function installCommand() {
  const name = $("browserChoice").value;
  const platform = $("platformChoice").value;
  if (platform.startsWith("linux")) {
    const arch = platform.endsWith("arm64") ? "arm64" : "amd64";
    $("installCommand").textContent =
      `sudo apt install ./mhm_${VERSION}_${arch}.deb\nmhm-setup install --browser ${name} --extension-id ${name === "firefox" ? FIREFOX_ID : chrome.runtime.id}`;
    return;
  }
  const windows = platform.startsWith("windows");
  const filename = `mhm-setup-${VERSION}-${platform}${windows ? ".exe" : ""}`;
  const binary = windows ? `.\\${filename}` : `./${filename}`;
  const command = `${binary} install --browser ${name} --extension-id ${name === "firefox" ? FIREFOX_ID : chrome.runtime.id}`;
  $("installCommand").textContent = windows
    ? command
    : `chmod +x ${binary}\n${command}`;
}
async function initialize() {
  try {
    const ping = await send("ping");
    if (
      !ping.mihomo ||
      !ping.security?.secure ||
      !ping.capabilities?.includes("mihomoProfiles") ||
      !ping.capabilities?.includes("subscriptionCertificates") ||
      !ping.capabilities?.includes("trafficLimits") ||
      !ping.capabilities?.includes("customRules")
    ) {
      navigate("onboardingView");
      $("setupDetail").textContent = t(
        !ping.security?.secure
          ? "secureStorageRequired"
          : "companionUpdateRequired",
      );
      return;
    }
    const result = await send("initialize");
    profiles = result.profiles || [];
    state = result.status;
    selectedProfileId = result.selectedProfileId;
    preferences = result.preferences;
    applyTheme(preferences.theme || "system");
    $("theme").value = preferences.theme || "system";
    $("killSwitch").checked = preferences.killSwitch !== false;
    $("mihomoPath").value = ping.settings?.mihomoPath || "";
    $("storageStatus").textContent =
      `${t("secureStorage")} · ${ping.security.backend}`;
    remoteOperation = result.operation;
    busy = localBusy || Boolean(remoteOperation);
    navigate(profiles.length ? "homeView" : "profilesView");
    render();
    await trafficView.refresh();
    if (state.status === "connected") await refreshGroups();
  } catch (error) {
    navigate("onboardingView");
    $("setupDetail").textContent = error.message;
  }
}
document.querySelectorAll("[data-route]").forEach((button) =>
  button.addEventListener("click", async () => {
    if (currentView === "editorView") {
      await leaveEditor();
      if (currentView === "editorView") return;
    }
    notice();
    navigate(button.dataset.route);
  }),
);
$("homeTraffic").addEventListener("click", () => navigate("trafficView"));
$("homeProfiles").addEventListener("click", () => navigate("profilesView"));
$("homeProfile").addEventListener("change", () =>
  run(async () => {
    selectedProfileId = $("homeProfile").value;
    await send("selectProfile", { profileId: selectedProfileId });
    render();
  }),
);
$("profileSearch").addEventListener("input", render);
$("profileForm").addEventListener("input", () => {
  dirty = true;
});
$("profileForm").addEventListener("change", () => {
  dirty = true;
});
$("sourceType").addEventListener("change", editorVisibility);
$("transport").addEventListener("change", editorVisibility);
$("tlsMode").addEventListener("change", editorVisibility);
$("certificateFile").addEventListener("change", () =>
  run(async () => {
    const file = $("certificateFile").files[0];
    if (!file) return;
    if (file.size > 256 * 1024) throw new Error(t("certificateTooLarge"));
    const data = new Uint8Array(await file.arrayBuffer());
    const text = new TextDecoder().decode(data);
    if (text.includes("-----BEGIN CERTIFICATE-----"))
      $("caCertificate").value = text;
    else {
      let binary = "";
      for (const byte of data) binary += String.fromCharCode(byte);
      $("caCertificate").value = "der:" + btoa(binary);
    }
    dirty = true;
  }),
);
$("showUrl").addEventListener("click", () => {
  const show = $("subscriptionUrl").type === "password";
  $("subscriptionUrl").type = show ? "text" : "password";
  $("showUrl").textContent = t(show ? "hideUrl" : "showUrl");
});
for (const [fileId, target] of [
  ["yamlFile", "yamlContent"],
  ["wireguardFile", "wireguardConfig"],
]) {
  $(fileId).addEventListener("change", () =>
    run(async () => {
      const file = $(fileId).files[0];
      if (!file) return;
      if (file.size > (fileId === "wireguardFile" ? 65536 : 8 * 1024 * 1024))
        throw new Error(t("fileTooLarge"));
      $(target).value = await file.text();
      dirty = true;
      if (!$("profileName").value && fileId === "yamlFile")
        $("profileName").value = file.name.replace(/\.ya?ml$/i, "");
    }),
  );
}
$("profileForm").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    let headers;
    try {
      headers = JSON.parse($("requestHeaders").value || "{}");
    } catch {
      throw new Error(t("invalidHeaders"));
    }
    const source = $("sourceType").value;
    const payload = {
      profileId: editingId,
      name: $("profileName").value.trim(),
      source,
      ...(source === "subscription"
        ? {
            url: $("subscriptionUrl").value.trim(),
            headers,
            tlsMode: $("tlsMode").value,
            caCertificate: $("caCertificate").value.trim(),
            transport: $("transport").value,
            updateIntervalMinutes: Number($("updateInterval").value),
            ...($("wireguardConfig").value.trim()
              ? { wireguard: $("wireguardConfig").value.trim() }
              : {}),
          }
        : { yaml: $("yamlContent").value }),
    };
    const result = await send("saveMihomoProfile", payload);
    profiles = result.profiles;
    selectedProfileId = result.profile.id;
    await send("selectProfile", { profileId: selectedProfileId });
    dirty = false;
    clearSecrets();
    navigate("homeView");
  }, t("profileSaved"));
});
$("updateProfile").addEventListener("click", () =>
  run(async () => {
    const result = await send("updateMihomoProfile", { profileId: editingId });
    profiles = result.profiles;
    dirty = false;
    clearSecrets();
    navigate("homeView");
  }, t("profileUpdated")),
);
$("rollbackProfile").addEventListener("click", () =>
  run(async () => {
    const result = await send("rollbackMihomoProfile", {
      profileId: editingId,
    });
    profiles = result.profiles;
    dirty = false;
    clearSecrets();
    navigate("homeView");
  }, t("profileRestored")),
);
$("deleteProfile").addEventListener("click", async () => {
  if (!(await confirmAction(t("deleteTitle"), t("deleteHint")))) return;
  run(async () => {
    const result = await send("deleteProfile", { profileId: editingId });
    profiles = result.profiles;
    dirty = false;
    clearSecrets();
    navigate("homeView");
  });
});
$("confirmCancel").addEventListener("click", () =>
  $("confirmDialog").close("cancel"),
);
$("confirmOk").addEventListener("click", () => $("confirmDialog").close("ok"));
$("editorBack").addEventListener("click", leaveEditor);
$("addProfile").addEventListener("click", () => run(() => openEditor()));
$("emptyAdd").addEventListener("click", () => run(() => openEditor()));
$("connectButton").addEventListener("click", () =>
  run(async () => {
    const selected = profiles.find((p) => p.id === selectedProfileId);
    if (selected?.needsTransportSetup) {
      await openEditor(selected);
      return;
    }
    if (selected?.needsDownload) {
      const result = await send("updateMihomoProfile", {
        profileId: selectedProfileId,
      });
      profiles = result.profiles;
      return;
    }
    if (state.status === "limited") {
      navigate("trafficView");
      return;
    }
    const disconnect =
      (["blocked", "limited"].includes(state.status) && !selected) ||
      (state.status === "connected" &&
        state.profileId === selectedProfileId &&
        state.revision === selected?.revision);
    const result = await send(
      disconnect ? "disconnect" : "connect",
      disconnect ? {} : { profileId: selectedProfileId },
    );
    state = disconnect ? { status: "disconnected" } : result;
    if (!disconnect) await refreshGroups();
  }),
);
$("refreshGroups").addEventListener("click", () => run(refreshGroups));
$("mode").addEventListener("change", () =>
  run(async () => {
    state = await send("setMode", { mode: $("mode").value });
    await refreshGroups();
  }),
);
$("settingsButton").addEventListener("click", async () => {
  if (currentView === "editorView") {
    await leaveEditor();
    if (currentView === "editorView") return;
  }
  notice();
  navigate("settingsView");
});
$("settingsBack").addEventListener("click", () => initialize());
document.querySelector(".brand").addEventListener("click", async (event) => {
  event.preventDefault();
  if (currentView === "editorView") await leaveEditor();
  else await initialize();
});
$("theme").addEventListener("change", () =>
  run(async () => {
    preferences.theme = $("theme").value;
    applyTheme(preferences.theme);
    await send("preferences", { preferences });
  }),
);
$("killSwitch").addEventListener("change", () =>
  run(async () => {
    preferences.killSwitch = $("killSwitch").checked;
    await send("preferences", { preferences });
  }),
);
$("language").addEventListener("change", async () => {
  await AppI18n.setLanguage($("language").value);
  translate();
  render();
});
$("saveCoreSettings").addEventListener("click", () =>
  run(async () => {
    await send("saveSettings", { mihomoPath: $("mihomoPath").value.trim() });
  }, t("saved")),
);
$("copyCommand").addEventListener("click", () =>
  run(
    () => navigator.clipboard.writeText($("installCommand").textContent),
    t("copied"),
  ),
);
$("copyId").addEventListener("click", () =>
  run(() => navigator.clipboard.writeText(chrome.runtime.id), t("copied")),
);
$("browserChoice").addEventListener("change", installCommand);
$("platformChoice").addEventListener("change", installCommand);
$("disconnectLink").addEventListener("click", async () => {
  try {
    await send("disconnect");
    state = { status: "disconnected", profileId: null };
    render();
  } catch (error) {
    notice(error.message, true);
  }
});
$("recheck").addEventListener("click", () => run(initialize));
$("openFullPage").addEventListener("click", () =>
  chrome.tabs.create({ url: chrome.runtime.getURL("setup.html") }),
);
chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== "stateChanged") return;
  state = message.state;
  profiles = message.profiles;
  selectedProfileId = message.selectedProfileId;
  remoteOperation = message.operation;
  busy = localBusy || Boolean(remoteOperation);
  render();
});
window.addEventListener("beforeunload", clearSecrets);
(async () => {
  await AppI18n.init();
  document.querySelectorAll(".version-label").forEach((node) => {
    node.textContent = `mhm ${VERSION} · powered by mihomo`;
  });
  translate();
  $("language").value = AppI18n.preference;
  $("extensionId").textContent = chrome.runtime.id;
  $("browserChoice").value = /Firefox/i.test(navigator.userAgent)
    ? "firefox"
    : /Edg\//i.test(navigator.userAgent)
      ? "edge"
      : /OPR/i.test(navigator.userAgent)
        ? "opera"
        : "chrome";
  $("platformChoice").value = /Win/i.test(navigator.platform)
    ? "windows-amd64"
    : /Linux/i.test(navigator.platform)
      ? "linux-amd64"
      : "macos-arm64";
  installCommand();
  await initialize();
  pollTimer = setInterval(async () => {
    if (
      document.hidden ||
      busy ||
      currentView !== "homeView" ||
      state.status !== "connected"
    )
      return;
    try {
      const result = await send("trafficStats");
      const session = result.traffic?.session;
      trafficView.renderBudget(result.traffic?.budget);
      $("downloadSpeed").textContent = `↓ ${byteRate(session?.downloadBps)}`;
      $("uploadSpeed").textContent = `↑ ${byteRate(session?.uploadBps)}`;
    } catch {
      /* Connection health is managed by the background worker. */
    }
  }, 2000);
})();
