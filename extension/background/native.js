/* Native Messaging transport. The controller owns connection policy. */
class MhmNativeClient {
  constructor(host, onDisconnect) {
    this.host = host;
    this.onDisconnect = onDisconnect;
    this.port = null;
    this.pending = new Map();
    this.sequence = 0;
  }
  open() {
    const port = chrome.runtime.connectNative(this.host);
    this.port = port;
    port.onMessage.addListener((message) => {
      const item = this.pending.get(message.requestId);
      if (!item) return;
      clearTimeout(item.timer);
      this.pending.delete(message.requestId);
      if (message.ok) item.resolve(message);
      else {
        const error = new Error(
          message.error || "The companion app could not complete this action.",
        );
        error.code = message.errorCode || "INTERNAL_ERROR";
        item.reject(error);
      }
    });
    port.onDisconnect.addListener(() => {
      const reason =
        chrome.runtime.lastError?.message || "The companion app disconnected.";
      if (this.port !== port) return;
      this.port = null;
      for (const item of this.pending.values()) {
        clearTimeout(item.timer);
        item.reject(new Error(reason));
      }
      this.pending.clear();
      this.onDisconnect(reason);
    });
  }
  request(command, payload = {}, timeout = 90000) {
    if (!this.port) this.open();
    return new Promise((resolve, reject) => {
      const requestId = `${Date.now()}-${++this.sequence}`;
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        const error = new Error(
          "The operation timed out. Check the companion app and try again.",
        );
        error.code = "TIMEOUT";
        reject(error);
      }, timeout);
      this.pending.set(requestId, { resolve, reject, timer });
      try {
        this.port.postMessage({
          protocolVersion: 1,
          requestId,
          command,
          ...payload,
        });
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(requestId);
        reject(error);
      }
    });
  }
}
