/* Toolbar presentation follows the same connection state as the UI. */
class MhmToolbar {
  constructor() {
    this.timer = null;
    this.status = null;
    this.frame = 0;
    this.budget = null;
  }
  update(state, profiles = [], budget) {
    if (!chrome.action?.setIcon) return;
    if (budget) this.budget = budget;
    const status = state.status;
    if (status !== this.status) {
      if (this.timer !== null) clearInterval(this.timer);
      this.timer = null;
      this.status = status;
      this.frame = 0;
      this.paint();
      if (status === "connecting")
        this.timer = setInterval(() => {
          this.frame = (this.frame + 1) % 3;
          this.paint();
        }, 350);
    }
    const keys = {
      connected: "toolbarConnected",
      connecting: "toolbarConnecting",
      blocked: "toolbarBlocked",
      limited: "toolbarLimited",
      disconnected: "toolbarDisconnected",
    };
    const label =
      chrome.i18n?.getMessage(keys[status] || keys.disconnected) || status;
    const profile = profiles.find((item) => item.id === state.profileId);
    chrome.action.setTitle({
      title: `mhm · ${label}${profile ? " · " + profile.name : ""}`,
    });
    const warning =
      status === "limited" || (status === "connected" && this.budget?.warning);
    chrome.action.setBadgeText({ text: warning ? "!" : "" });
    if (warning)
      chrome.action.setBadgeBackgroundColor({
        color: status === "limited" ? "#B62F42" : "#96630A",
      });
  }
  paint() {
    const name =
      this.status === "connecting"
        ? `connecting-${this.frame}`
        : ["blocked", "limited"].includes(this.status)
          ? "blocked"
          : this.status === "connected"
            ? "connected"
            : "disconnected";
    chrome.action.setIcon({
      path: {
        16: `icons/status-${name}-16.png`,
        32: `icons/status-${name}-32.png`,
      },
    });
  }
}
