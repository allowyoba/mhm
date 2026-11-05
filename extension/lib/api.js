/* Browser UI boundary. Source material is sent only to the background controller. */
export const $ = (id) => document.getElementById(id);
export async function send(action, payload = {}) {
  const response = await chrome.runtime.sendMessage({ action, ...payload });
  if (!response?.ok) {
    const error = new Error(response?.error || AppI18n.t("unknownError"));
    error.code = response?.errorCode || "INTERNAL_ERROR";
    throw error;
  }
  return response;
}
