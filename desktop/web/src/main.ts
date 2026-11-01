import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import { createPinia } from "pinia";
import App from "./App.vue";
import "./styles.css";
import "./design/system.css";
import { initializeLocalSession } from "./domain/localSession";

// Vue Router snapshots the current URL when its module is initialized.  The
// one-use bootstrap fragment must be consumed before creating that history,
// otherwise a later router navigation can restore the already-used token.
void initializeLocalSession().then(async () => {
  const { default: router } = await import("./router");
  createApp(App).use(createPinia()).use(router).use(ElementPlus).mount("#app");
}).catch((error: unknown) => {
  const root = document.getElementById("app");
  if (!root) return;
  const panel = document.createElement("main"); panel.className = "session-entry"; panel.setAttribute("role", "alert");
  const title = document.createElement("h1"); title.textContent = "打开MarketListener本机会话";
  const message = document.createElement("p"); message.textContent = error instanceof Error ? error.message : "本机会话无法建立，请从快捷入口重新打开。";
  const hint = document.createElement("p"); hint.textContent = "使用桌面的产品快捷入口，或已授权管理台中的“打开客户页面”。会话不需要额外账号密码。";
  panel.append(title, message, hint); root.replaceChildren(panel);
});
