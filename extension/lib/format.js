export function formatBytes(value) {
  let number = Math.max(0, Number(value) || 0);
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return `${unit ? number.toFixed(number < 10 ? 1 : 0) : Math.round(number)} ${units[unit]}`;
}
export const formatRate = (value) => `${formatBytes(value)}/s`;
export function formatDate(value, locale, time = false) {
  const date =
    typeof value === "number"
      ? new Date(value * 1000)
      : new Date(`${value}T12:00:00`);
  return time
    ? date.toLocaleString(locale, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : date.toLocaleDateString(locale);
}
