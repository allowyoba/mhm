import { $, send } from "./api.js";

export function createRulesView({
  t,
  run,
  confirmDiscard,
  onProfilesChanged,
  onApply,
  getState,
}) {
  let profileId = null,
    rules = [],
    dirty = false,
    loaded = false;
  function render() {
    $("ruleList").replaceChildren();
    $("rulesEmpty").classList.toggle("hidden", rules.length > 0);
    $("rulesDirty").textContent = dirty ? t("unsavedChanges") : "";
    $("rawRules").value = rules.join("\n");
    for (const [index, rule] of rules.entries()) {
      const row = document.createElement("div");
      row.className = "rule-row";
      const text = document.createElement("code");
      text.textContent = rule;
      row.append(text);
      for (const [label, symbol, offset] of [
        ["moveUp", "↑", -1],
        ["moveDown", "↓", 1],
        ["removeRule", "×", 0],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "text-button";
        button.textContent = symbol;
        button.setAttribute("aria-label", t(label));
        button.disabled =
          offset !== 0 &&
          (index + offset < 0 || index + offset >= rules.length);
        button.addEventListener("click", () => {
          if (offset)
            [rules[index], rules[index + offset]] = [
              rules[index + offset],
              rules[index],
            ];
          else rules.splice(index, 1);
          dirty = true;
          render();
        });
        row.append(button);
      }
      $("ruleList").append(row);
    }
  }
  function sync(profiles, state) {
    const profile = profiles.find((item) => item.id === profileId);
    $("applyRules").classList.toggle(
      "hidden",
      !profile ||
        state.status !== "connected" ||
        state.profileId !== profileId ||
        state.revision === profile.revision,
    );
  }
  async function load(id) {
    if (!id) return;
    const result = await send("profileRules", { profileId: id });
    profileId = id;
    rules = result.rules;
    dirty = false;
    loaded = true;
    $("ruleTarget").replaceChildren();
    for (const target of result.targets) {
      const option = document.createElement("option");
      option.value = target;
      option.textContent = target;
      $("ruleTarget").append(option);
    }
    render();
  }
  async function refresh(profiles, selectedId) {
    const available = profiles.filter((item) => !item.needsDownload);
    const next = available.some((p) => p.id === profileId)
      ? profileId
      : available.find((p) => p.id === selectedId)?.id || available[0]?.id;
    $("rulesProfile").replaceChildren();
    for (const profile of available) {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = profile.name;
      $("rulesProfile").append(option);
    }
    $("rulesProfile").value = next || "";
    $("rulesEditor").classList.toggle("hidden", !next);
    $("noRulesProfile").classList.toggle("hidden", !!next);
    if (next && (!loaded || next !== profileId)) await load(next);
    sync(profiles, getState());
  }
  $("rulesProfile").addEventListener("change", async () => {
    const next = $("rulesProfile").value;
    if (dirty && !(await confirmDiscard())) {
      $("rulesProfile").value = profileId;
      return;
    }
    run(() => load(next));
  });
  $("ruleType").addEventListener("change", () => {
    $("ruleValue").disabled = $("ruleType").value === "MATCH";
    $("ruleValue").required = $("ruleType").value !== "MATCH";
  });
  $("ruleBuilder").addEventListener("submit", (event) => {
    event.preventDefault();
    const type = $("ruleType").value,
      target = $("ruleTarget").value;
    let value = $("ruleValue").value.trim();
    if (type.startsWith("DOMAIN") && /^https?:\/\//i.test(value)) {
      try {
        value = new URL(value).hostname;
      } catch {
        return;
      }
    }
    if (value.includes(",") || value.includes("\n")) return;
    rules.push(
      type === "MATCH"
        ? `MATCH,${target}`
        : `${type},${value},${target}${type.startsWith("IP-CIDR") ? ",no-resolve" : ""}`,
    );
    dirty = true;
    $("ruleValue").value = "";
    render();
  });
  $("rawRules").addEventListener("input", () => {
    rules = $("rawRules")
      .value.split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    dirty = true;
    $("rulesDirty").textContent = t("unsavedChanges");
  });
  $("rawRules").addEventListener("change", render);
  $("saveRules").addEventListener("click", () =>
    run(async () => {
      const response = await send("saveProfileRules", { profileId, rules });
      rules = response.rules;
      dirty = false;
      onProfilesChanged(response.profiles);
      render();
      sync(response.profiles, getState());
    }, t("rulesSaved")),
  );
  $("applyRules").addEventListener("click", () =>
    run(() => onApply(profileId), t("rulesApplied")),
  );
  return { refresh, sync };
}
