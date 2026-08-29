<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useThemeStore } from "./stores/theme";

const theme = useThemeStore();
const route = useRoute();

const client = [
  ["/market/", "行情"],
  ["/settings/", "设置"],
  ["/data/", "数据"],
  ["/futures/", "期货数据"],
  ["/strategy/", "策略"],
  ["/stats/", "账户分析"],
  ["/industry/", "产业链"],
] as const;

const backend = [
  ["/", "首页"],
  ["/data-sources/", "数据源"],
  ["/f10/", "F10"],
  ["/logs/", "日志"],
] as const;

const activePath = computed(() => route.path);
const isActive = (path: string): boolean =>
  path === "/" ? activePath.value === "/" : activePath.value.startsWith(path);

function chooseTheme(mode: string): void {
  if (mode === "system" || mode === "light" || mode === "dark") theme.setMode(mode);
}
</script>

<template>
  <el-container class="terminal">
    <el-header class="topbar">
      <router-link class="brand" to="/" data-test="brand">MarketListener</router-link>
      <nav class="nav-group workbench-nav" aria-label="客户端">
        <span class="nav-label">客户端</span>
        <router-link
          v-for="[path, label] in client"
          :key="path"
          :to="path"
          :class="{ active: isActive(path) }"
        >{{ label }}</router-link>
      </nav>
      <nav class="nav-group management-nav" aria-label="后端">
        <span class="nav-label">后端</span>
        <router-link
          v-for="[path, label] in backend"
          :key="path"
          :to="path"
          :class="{ active: isActive(path) }"
        >{{ label }}</router-link>
      </nav>
      <el-dropdown trigger="click" class="theme-menu" @command="chooseTheme">
        <button type="button" class="theme-button" data-test="theme-toggle">
          <span class="theme-dot" :class="theme.effective" />
          主题
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="system" data-test="theme-option-system">跟随系统</el-dropdown-item>
            <el-dropdown-item command="light" data-test="theme-option-light">浅色</el-dropdown-item>
            <el-dropdown-item command="dark" data-test="theme-option-dark">深色</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-header>
    <el-main :class="{ 'market-main': isActive('/market/') }"><router-view /></el-main>
  </el-container>
</template>
