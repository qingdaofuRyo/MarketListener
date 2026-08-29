import { createRouter, createWebHistory } from "vue-router";
const HomeView = () => import("./views/HomeView.vue");
const DataView = () => import("./views/DataView.vue");
const DataSourcesView = () => import("./views/DataSourcesView.vue");
const F10View = () => import("./views/F10View.vue");
const FuturesView = () => import("./views/FuturesView.vue");
const IndustryView = () => import("./views/IndustryView.vue");
const LogsView = () => import("./views/LogsView.vue");
const MarketView = () => import("./views/MarketView.vue");
const SettingsView = () => import("./views/SettingsView.vue");
const StrategyView = () => import("./views/StrategyView.vue");
const StatsView = () => import("./views/StatsView.vue");

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: HomeView },
    { path: "/market/", component: MarketView },
    { path: "/settings/", component: SettingsView },
    { path: "/data/", component: DataView },
    { path: "/data-sources/", component: DataSourcesView },
    { path: "/strategy/", component: StrategyView },
    { path: "/stats/", component: StatsView },
    { path: "/f10/", component: F10View },
    { path: "/f10/company/:instrumentKey", component: F10View, props: true },
    { path: "/futures/", component: FuturesView },
    { path: "/industry/", component: IndustryView },
    { path: "/logs/", component: LogsView },
    { path: "/industry-v2/", redirect: "/industry/" },
  ],
});
