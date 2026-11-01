<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useThemeStore } from "./stores/theme";

const theme = useThemeStore();
const route = useRoute();
const router = useRouter();
const commandOpen = ref(false);
const commandQuery = ref("");
const commandIndex = ref(0);
const commandInput = ref<HTMLInputElement | null>(null);
const workspaceNav = ref<HTMLElement | null>(null);

const client = [
  ["/market/all/", "全部行情"],
  ["/market/targets/", "目标行情"],
  ["/settings/", "设置"],
  ["/data/", "数据"],
  ["/futures/", "期货数据"],
  ["/strategy/", "策略"],
  ["/stats/", "账户分析"],
  ["/industry/", "产业链"],
  ["/quant/", "金工"],
  ["/f10/", "F10"],
] as const;

const activePath = computed(() => route.path);
const isActive = (path: string): boolean =>
  path === "/" ? activePath.value === "/" : activePath.value.startsWith(path);

const currentWorkspace = computed(() => client.find(([path]) => isActive(path))?.[1] || "研究终端");
watch(activePath, async () => {
  await nextTick();
  workspaceNav.value?.querySelector<HTMLElement>('[aria-current="page"]')
    ?.scrollIntoView({ block: "nearest", inline: "nearest" });
});
const pageDescriptions: Record<(typeof client)[number][0], string> = {
  "/market/all/": "全市场报价与K线",
  "/market/targets/": "关注标的与扫描结果",
  "/settings/": "外观与工作区偏好",
  "/data/": "来源、覆盖与数据状态",
  "/futures/": "热度、分布与席位",
  "/strategy/": "策略研究与运行记录",
  "/stats/": "账户流水与绩效",
  "/industry/": "产业关系与证据",
  "/quant/": "方法、工具与本地实验",
  "/f10/": "公司资料与财务",
};
const commands = [
  ...client.map(([path, label]) => ({ id: path, label, detail: pageDescriptions[path], type: "page" as const, value: path })),
  { id: "theme-light", label: "浅色主题", detail: "切换外观", type: "theme" as const, value: "light" },
  { id: "theme-dark", label: "深色主题", detail: "切换外观", type: "theme" as const, value: "dark" },
  { id: "theme-system", label: "跟随系统主题", detail: "切换外观", type: "theme" as const, value: "system" },
];
const visibleCommands = computed(() => {
  const words = commandQuery.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return commands.filter((item) => words.every((word) => `${item.label} ${item.value}`.toLocaleLowerCase().includes(word)));
});

watch(commandQuery, () => { commandIndex.value = 0; });

function openCommands(): void {
  commandQuery.value = "";
  commandIndex.value = 0;
  commandOpen.value = true;
  void nextTick(() => commandInput.value?.focus());
}

function executeCommand(index: number): void {
  const command = visibleCommands.value[index];
  if (!command) return;
  commandOpen.value = false;
  if (command.type === "page") void router.push(command.value);
  else chooseTheme(command.value);
}

function moveCommand(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter") {
    event.preventDefault();
    executeCommand(commandIndex.value);
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const count = visibleCommands.value.length;
    if (!count) return;
    commandIndex.value = (commandIndex.value + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
    void nextTick(() => document.getElementById(`workspace-command-${commandIndex.value}`)?.scrollIntoView({ block: "nearest" }));
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    if (!visibleCommands.value.length) return;
    commandIndex.value = event.key === "Home" ? 0 : visibleCommands.value.length - 1;
    void nextTick(() => document.getElementById(`workspace-command-${commandIndex.value}`)?.scrollIntoView({ block: "nearest" }));
  }
}

function workspaceShortcut(event: KeyboardEvent): void {
  if (event.key === "Escape" && commandOpen.value) {
    event.preventDefault();
    event.stopImmediatePropagation();
    commandOpen.value = false;
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (event.repeat) return;
    if (commandOpen.value) commandOpen.value = false;
    else openCommands();
  }
}

onMounted(() => window.addEventListener("keydown", workspaceShortcut, true));
onUnmounted(() => window.removeEventListener("keydown", workspaceShortcut, true));

function chooseTheme(mode: string): void {
  if (mode === "system" || mode === "light" || mode === "dark") theme.setMode(mode);
}
</script>

<template>
  <a class="workspace-skip" href="#workbench-content">跳到工作区</a>
  <el-container class="terminal">
    <el-header class="topbar">
      <router-link class="brand" to="/market/all/" data-test="brand" aria-label="MarketListener 全部行情">
        <span class="brand-mark" aria-hidden="true"><i /><i /><i /></span>
        <span>MarketListener</span>
      </router-link>
      <nav ref="workspaceNav" class="nav-group workbench-nav" aria-label="客户端">
        <router-link
          v-for="[path, label] in client"
          :key="path"
          :to="path"
          :class="{ active: isActive(path) }"
          :aria-current="isActive(path) ? 'page' : undefined"
        >{{ label }}</router-link>
      </nav>
      <div class="workspace-tools">
        <button type="button" class="workspace-command-trigger" aria-label="搜索页面和主题，Ctrl加K" :aria-expanded="commandOpen" @click="openCommands">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></svg>
          <span>搜索</span><kbd>Ctrl K</kbd>
        </button>
      <el-dropdown trigger="click" class="theme-menu" @command="chooseTheme">
        <button type="button" class="theme-button" data-test="theme-toggle" aria-label="选择外观主题">
          <span class="theme-dot" :class="theme.effective" />
          <span class="theme-caption">主题</span>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="system" data-test="theme-option-system">跟随系统</el-dropdown-item>
            <el-dropdown-item command="light" data-test="theme-option-light">浅色</el-dropdown-item>
            <el-dropdown-item command="dark" data-test="theme-option-dark">深色</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      </div>
    </el-header>
    <el-main id="workbench-content" tabindex="-1" :aria-label="currentWorkspace" :class="{ 'market-main': isActive('/market/') }"><router-view /></el-main>
  </el-container>
  <el-dialog v-model="commandOpen" title="快速前往" class="workspace-command-dialog" width="min(540px, 92vw)" :z-index="3400" align-center @opened="commandInput?.focus()">
    <div class="workspace-command-search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></svg>
      <input ref="commandInput" v-model="commandQuery" role="combobox" aria-label="搜索页面或主题" aria-autocomplete="list" aria-controls="workspace-command-list" :aria-expanded="true" :aria-activedescendant="visibleCommands.length ? `workspace-command-${commandIndex}` : undefined" placeholder="搜索页面或切换主题" autocomplete="off" @keydown="moveCommand" />
      <kbd>Esc</kbd>
    </div>
    <div id="workspace-command-list" class="workspace-command-list" role="listbox" aria-label="页面和主题">
      <button v-for="(command, index) in visibleCommands" :id="`workspace-command-${index}`" :key="command.id" type="button" role="option" tabindex="-1" :aria-selected="index === commandIndex" :class="{ selected: index === commandIndex }" @pointermove="commandIndex = index" @click="executeCommand(index)">
        <span class="command-kind" aria-hidden="true">{{ command.type === 'page' ? '↗' : '◐' }}</span>
        <span class="command-name">{{ command.label }}<small>{{ command.detail }}</small></span>
        <span class="command-enter" aria-hidden="true">↵</span>
      </button>
      <p v-if="!visibleCommands.length" class="workspace-command-empty">没有匹配项，试试“期货”或“策略”。</p>
    </div>
    <div class="workspace-command-hint"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Home</kbd><kbd>End</kbd> 首尾</span><span><kbd>Enter</kbd> 打开</span></div>
  </el-dialog>
</template>
