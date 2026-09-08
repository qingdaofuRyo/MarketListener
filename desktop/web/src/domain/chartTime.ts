import { weekdayLabel } from "./marketList";

function displayedDate(value: string): string | null {
  const day = value.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const timestamp = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === day ? day : null;
}

/** Standard bars carry their display date/offset; never substitute trading day or today's date. */
export function formatCrosshairTime(value: string, period: string): string {
  const day = displayedDate(value);
  if (!day) return "—";
  const time = /^\d{2}:\d{2}/.exec(value.slice(11))?.[0];
  return `${day} ${weekdayLabel(day)}${time && ["5m", "15m", "30m", "1h", "2h"].includes(period) ? ` ${time}` : ""}`;
}

export function formatAxisDate(value: string, period: string): string {
  const day = displayedDate(value);
  return day ? (period === "1y" ? day.slice(0, 4) : day.slice(5)) : "—";
}
