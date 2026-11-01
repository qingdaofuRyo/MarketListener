import type { Palette } from "../design/tokens";

export function futuresTooltip(palette: Palette) {
  return {
    confine: true, padding: [12, 15], borderWidth: 1, transitionDuration: .12,
    backgroundColor: palette.chartTooltip, borderColor: palette.chartTooltipBorder,
    textStyle: { color: palette.textPrimary, fontSize: 12, lineHeight: 23 },
    extraCssText: "border-radius:12px;box-shadow:0 10px 32px rgba(20,30,55,.16);backdrop-filter:blur(12px);",
  };
}

export function futuresAxisPointer(palette: Palette) {
  return {
    type: "cross", snap: true,
    lineStyle: { color: palette.chartSecondary, type: "dashed", width: 1 },
    crossStyle: { color: palette.chartSecondary, type: "dashed", width: 1 },
    label: { color: palette.textPrimary, backgroundColor: palette.surfaceSelected, padding: [5, 8] },
  };
}

export function futuresLegend(palette: Palette) {
  return { type: "scroll", top: 2, icon: "roundRect", itemWidth: 14, itemHeight: 5, itemGap: 20,
    textStyle: { color: palette.textSecondary, fontSize: 12 } };
}

export function futuresZoom(palette: Palette) {
  return [{ type: "inside", filterMode: "none" }, {
    type: "slider", height: 20, bottom: 3, filterMode: "none", showDetail: false,
    borderColor: palette.divider, backgroundColor: palette.surfaceSelected,
    fillerColor: palette.accentSoft, dataBackground: { lineStyle: { color: palette.chartSecondary }, areaStyle: { color: palette.surface } },
    selectedDataBackground: { lineStyle: { color: palette.accent }, areaStyle: { color: palette.accentSoft } },
    handleSize: "110%", handleStyle: { color: palette.surfaceElevated, borderColor: palette.accent, borderWidth: 1.5 },
    moveHandleSize: 0, brushSelect: false,
  }];
}
