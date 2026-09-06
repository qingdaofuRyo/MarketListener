<script setup lang="ts">
defineProps<{ name: string }>();
const paths: Record<string, string> = {
  candles: 'M6 2v4m0 10v6M3 6h6v10H3zM17 2v8m0 8v4M14 10h6v8h-6z',
  hollow: 'M6 2v4m0 10v6M3 6h6v10H3zM17 2v8m0 8v4M14 10h6v8h-6z',
  heikin: 'M5 2v20M2 6h6v8H2zM16 2v20M13 9h6v8h-6zM1 20L22 3',
  line: 'M2 18l5-7 5 3 9-11', area: 'M2 18l5-7 5 3 9-11v18H2z',
  indicator: 'M4 20V4h16M7 16l4-6 4 3 5-8', colors: 'M3 7h16l-4-4M21 17H5l4 4',
  flip: 'M12 2v20M8 6l4-4 4 4M8 18l4 4 4-4M3 12h18', replay: 'M5 5v14M20 5L9 12l11 7z',
  play: 'M7 4l14 8-14 8z', pause: 'M7 4v16M17 4v16', step: 'M5 5l10 7-10 7zM19 5v14',
  forward: 'M2 5l9 7-9 7zM12 5l9 7-9 7z', start: 'M12 2v20M2 12h20M8 8l4-4 4 4',
  close: 'M5 5l14 14M19 5L5 19', visible: 'M2 12q10-14 20 0-10 14-20 0M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  hidden: 'M2 12q10-14 20 0-10 14-20 0M3 3l18 18', periods: 'M3 6h18v15H3zM7 2v7M17 2v7M3 11h18',
  delete: 'M3 6h18M8 6V3h8v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  settings: 'M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6',
  chevron: 'M6 9l6 6 6-6', long: 'M3 21V3h18M4 15h16M4 7h16M7 12l5-5 5 5M12 7v12',
  short: 'M3 21V3h18M4 9h16M4 17h16M7 12l5 5 5-5M12 5v12',
};
</script>
<template><svg class="chart-icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="paths[name] || paths.settings" :fill="['candles','area'].includes(name) ? 'currentColor' : 'none'" /></svg></template>
<style scoped>.chart-icon{width:19px;height:19px;flex:none;vertical-align:middle;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}</style>
