<script setup lang="ts">
withDefaults(
  defineProps<{
    density?: 'dense' | 'standard' | 'comfortable';
    stickyHeader?: boolean;
    minWidth?: string;
    ariaLabel?: string;
  }>(),
  {
    density: 'dense',
    stickyHeader: true,
    minWidth: '100%',
    ariaLabel: '数据表格',
  },
);
</script>

<template>
  <div
    class="terminal-table"
    :class="[
      `terminal-table--${density}`,
      { 'terminal-table--sticky': stickyHeader },
    ]"
    :style="{ '--terminal-table-min-width': minWidth }"
    role="table"
    :aria-label="ariaLabel"
  >
    <div v-if="$slots.header" class="terminal-table__header" role="rowgroup">
      <slot name="header" />
    </div>
    <div class="terminal-table__body" role="rowgroup">
      <slot />
    </div>
    <div v-if="$slots.footer" class="terminal-table__footer">
      <slot name="footer" />
    </div>
  </div>
</template>

<style scoped>
.terminal-table {
  width: 100%;
  min-width: 0;
  color: var(--ml-text-primary);
  background: var(--ml-surface);
  border: 1px solid var(--ml-divider);
  border-radius: var(--ml-radius-panel);
  overflow: auto;
}

.terminal-table__header,
.terminal-table__body,
.terminal-table__footer {
  min-width: var(--terminal-table-min-width, 100%);
}

.terminal-table__header {
  background: var(--ml-surface-elevated);
  border-bottom: 1px solid var(--ml-divider);
  color: var(--ml-text-secondary);
  font-weight: 600;
}

.terminal-table--sticky .terminal-table__header {
  position: sticky;
  top: 0;
  z-index: var(--ml-layer-sticky);
}

.terminal-table--dense {
  font-size: 12px;
  line-height: var(--ml-line-height-dense);
}

.terminal-table--standard {
  font-size: 13px;
  line-height: var(--ml-line-height-normal);
}

.terminal-table--comfortable {
  font-size: 14px;
  line-height: var(--ml-line-height-normal);
}

.terminal-table__footer {
  border-top: 1px solid var(--ml-divider);
  color: var(--ml-text-secondary);
}
</style>
