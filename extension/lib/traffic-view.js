import { $, send } from "./api.js";
import { formatBytes, formatDate } from "./format.js";

export function createTrafficView({ t, run }) {
  let dirty = false;
  const locale = () => AppI18n.locale.replace("_", "-");
  function visibility() {
    $("limitFields").classList.toggle("hidden", !$("limitEnabled").checked);
    $("cycleDayField").classList.toggle(
      "hidden",
      $("limitPeriod").value !== "month",
    );
  }
  function renderBudget(budget) {
    if (!budget) return;
    const text = `${formatBytes(budget.usedBytes)} / ${formatBytes(budget.limitBytes)} · ${t("renews")} ${formatDate(budget.endDay, locale())}`;
    $("trafficBudget").textContent = budget.enabled ? text : t("limitDisabled");
    $("budgetProgress").classList.toggle("hidden", !budget.enabled);
    $("budgetProgress").value = Math.min(100, budget.percent);
    $("homeBudget").classList.toggle("hidden", !budget.enabled);
    $("homeBudget").classList.toggle("budget-danger", budget.blocked);
    $("homeBudget").textContent =
      `${budget.blocked ? t("limitReached") + " · " : ""}${text}`;
  }
  function chart(series) {
    const root = $("trafficChart");
    root.replaceChildren();
    const ns = "http://www.w3.org/2000/svg";
    const max = Math.max(
      1,
      ...series.map((day) => day.downloadBytes + day.uploadBytes),
    );
    let total = 0;
    for (const [index, day] of series.entries()) {
      const bytes = day.downloadBytes + day.uploadBytes;
      total += bytes;
      const height = Math.max(2, (bytes / max) * 115);
      const bar = document.createElementNS(ns, "rect");
      bar.setAttribute("x", String(index * 20 + 3));
      bar.setAttribute("y", String(125 - height));
      bar.setAttribute("width", "14");
      bar.setAttribute("height", String(height));
      bar.setAttribute("rx", "3");
      bar.setAttribute("class", "traffic-bar");
      const title = document.createElementNS(ns, "title");
      title.textContent = `${formatDate(day.day, locale())} · ${formatBytes(bytes)}`;
      bar.append(title);
      root.append(bar);
    }
    $("trafficChartSummary").textContent =
      `${t("last30Days")} · ${formatBytes(total)}`;
  }
  function sessions(items) {
    $("trafficSessions").replaceChildren();
    $("noSessions").classList.toggle("hidden", items.length > 0);
    for (const item of items) {
      const row = document.createElement("article");
      row.className = "session-row";
      const text = document.createElement("div"),
        name = document.createElement("strong"),
        date = document.createElement("small");
      name.textContent = item.profileName;
      date.textContent = `${formatDate(item.startedAt, locale(), true)}${item.endedAt ? "" : " · " + t("sessionActive")}`;
      text.append(name, date);
      const amount = document.createElement("span");
      amount.textContent = formatBytes(item.downloadBytes + item.uploadBytes);
      row.append(text, amount);
      $("trafficSessions").append(row);
    }
  }
  async function refresh() {
    const { traffic } = await send("trafficOverview");
    for (const [id, key] of [
      ["trafficToday", "today"],
      ["trafficMonth", "month"],
      ["trafficAll", "allTime"],
    ]) {
      $(id).textContent = formatBytes(
        traffic[key].downloadBytes + traffic[key].uploadBytes,
      );
    }
    chart(traffic.series);
    sessions(traffic.sessions);
    renderBudget(traffic.budget);
    if (!dirty) {
      const policy = traffic.budget;
      $("limitEnabled").checked = policy.enabled;
      $("limitGiB").value = String(policy.limitBytes / 1024 ** 3);
      $("limitPeriod").value = policy.period;
      $("cycleDay").value = String(policy.cycleDay);
      $("limitAction").value = policy.action;
      visibility();
    }
  }
  $("limitEnabled").addEventListener("change", visibility);
  $("limitPeriod").addEventListener("change", visibility);
  $("trafficPolicyForm").addEventListener("input", () => {
    dirty = true;
  });
  $("trafficPolicyForm").addEventListener("change", () => {
    dirty = true;
  });
  $("trafficPolicyForm").addEventListener("submit", (event) => {
    event.preventDefault();
    run(async () => {
      $("trafficPolicyFields").disabled = true;
      try {
        const policy = {
          enabled: $("limitEnabled").checked,
          limitBytes: Math.round(Number($("limitGiB").value) * 1024 ** 3),
          period: $("limitPeriod").value,
          cycleDay: Number($("cycleDay").value),
          action: $("limitAction").value,
        };
        await send("saveTrafficPolicy", { policy });
        dirty = false;
        await refresh();
      } finally {
        $("trafficPolicyFields").disabled = false;
      }
    }, t("limitSaved"));
  });
  $("refreshTraffic").addEventListener("click", () => run(refresh));
  return { refresh, renderBudget };
}
