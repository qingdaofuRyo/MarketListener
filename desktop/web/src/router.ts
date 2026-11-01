import { createRouter, createWebHistory } from "vue-router";
const DataView = () => import("./views/DataView.vue");
const F10View = () => import("./views/F10View.vue");
const FuturesView = () => import("./views/FuturesView.vue");
const IndustryView = () => import("./views/IndustryView.vue");
const QuantView = () => import("./views/QuantView.vue");
const AllMarketView = () => import("./views/AllMarketView.vue");
const TargetMarketView = () => import("./views/TargetMarketView.vue");
const MarketInstrumentView = () => import("./views/MarketInstrumentView.vue");
const SettingsView = () => import("./views/SettingsView.vue");
const StrategyView = () => import("./views/StrategyView.vue");
const StatsView = () => import("./views/StatsView.vue");

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/market/all/" },
    { path: "/market/", redirect: "/market/all/" },
    { path: "/market/all/", component: AllMarketView },
    { path: "/market/targets/", component: TargetMarketView },
    { path: "/market/instrument/:instrumentId/", component: MarketInstrumentView, props: true },
    { path: "/settings/", component: SettingsView },
    { path: "/data/", component: DataView },
    { path: "/data-sources/", redirect: "/market/all/" },
    { path: "/strategy/", component: StrategyView },
    { path: "/stats/", component: StatsView },
    { path: "/f10/", component: F10View },
    { path: "/f10/company/:instrumentKey", component: F10View, props: true },
    { path: "/futures/", component: FuturesView },
    { path: "/industry/", component: IndustryView },
    { path: "/quant/", component: QuantView },
    { path: "/logs/", redirect: "/market/all/" },
    { path: "/industry-v2/", redirect: "/industry/" },
  ],
});
