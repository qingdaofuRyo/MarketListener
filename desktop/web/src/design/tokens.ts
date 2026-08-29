/** MarketListener terminal design tokens.
 *
 * 单一定义金融终端语义色：上涨红、下跌绿、平盘灰；深/浅两套完整 Palette。
 * 页面与图表一律从这里取色，禁止在组件里散落硬编码 RGB。
 */

export type ThemeMode = "system" | "light" | "dark";
export type EffectiveTheme = "light" | "dark";

export interface Palette {
  background: string;
  surface: string;
  surfaceElevated: string;
  surfaceSelected: string;
  divider: string;
  textPrimary: string;
  textSecondary: string;
  textDisabled: string;
  accent: string;
  accentSoft: string;
  priceUp: string;
  priceDown: string;
  flat: string;
  warning: string;
  error: string;
  info: string;
  highlight: string;
  chartGrid: string;
  chartAxis: string;
  chartTooltip: string;
  chartTooltipBorder: string;
  heatTotal: string;
  heatBreadth: string;
  heatFund: string;
  heatExtremeShort: string;
  heatShort: string;
  heatMildShort: string;
  heatNeutral: string;
  heatMildLong: string;
  heatLong: string;
  heatExtremeLong: string;
}

export const palettes: Record<EffectiveTheme, Palette> = {
  dark: {
    background: "#0b0e14",
    surface: "#10151e",
    surfaceElevated: "#151c27",
    surfaceSelected: "#1b2534",
    divider: "#232d3d",
    textPrimary: "#e8edf5",
    textSecondary: "#929eaf",
    textDisabled: "#586273",
    accent: "#2962ff",
    accentSoft: "#1e3a8a",
    priceUp: "#f0414e",
    priceDown: "#22b07d",
    flat: "#929eaf",
    warning: "#f5a623",
    error: "#ff5252",
    info: "#4f8cff",
    highlight: "#ffd166",
    chartGrid: "#232d3d",
    chartAxis: "#7c8899",
    chartTooltip: "#1b2534",
    chartTooltipBorder: "#31415c",
    heatTotal: "#5b8cff",
    heatBreadth: "#f6c453",
    heatFund: "#b692f6",
    heatExtremeShort: "#0b6b4f",
    heatShort: "#12976b",
    heatMildShort: "#55c89a",
    heatNeutral: "#5f6b7a",
    heatMildLong: "#f58b94",
    heatLong: "#ef4b57",
    heatExtremeLong: "#b91c2c",
  },
  light: {
    background: "#f5f7fa",
    surface: "#ffffff",
    surfaceElevated: "#f0f3f8",
    surfaceSelected: "#e8eef8",
    divider: "#e1e6ee",
    textPrimary: "#11151c",
    textSecondary: "#687386",
    textDisabled: "#9aa5b4",
    accent: "#2962ff",
    accentSoft: "#dbe6ff",
    priceUp: "#d92d20",
    priceDown: "#0e9f6e",
    flat: "#687386",
    warning: "#b45309",
    error: "#dc2626",
    info: "#2563eb",
    highlight: "#b45309",
    chartGrid: "#e1e6ee",
    chartAxis: "#687386",
    chartTooltip: "#ffffff",
    chartTooltipBorder: "#d7dee9",
    heatTotal: "#1d4ed8",
    heatBreadth: "#a16207",
    heatFund: "#7e22ce",
    heatExtremeShort: "#065f46",
    heatShort: "#0e9f6e",
    heatMildShort: "#7fcdb0",
    heatNeutral: "#94a3b8",
    heatMildLong: "#f5a3a9",
    heatLong: "#d92d20",
    heatExtremeLong: "#991b1b",
  },
};

export function applyTokens(theme: EffectiveTheme): void {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.classList.toggle("dark", theme === "dark");
  const palette = palettes[theme];
  for (const [name, value] of Object.entries(palette)) {
    const variable = name.replace(/[A-Z]/g, (char) => `-${char.toLowerCase()}`);
    root.style.setProperty(`--ml-${variable}`, value);
  }
}
