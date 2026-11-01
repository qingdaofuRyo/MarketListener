import { copyFile, mkdir, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const source = path.dirname(fileURLToPath(import.meta.url));
const destination = path.resolve(source, "../src/market_monitor/admin_web_dist");
const files = [
  ["index.html", "index.html"], ["src/admin.js", "admin.js"],
  ["src/admin.css", "admin.css"], ["public/favicon.svg", "favicon.svg"],
  ["public/THIRD_PARTY_LICENSES.md", "THIRD_PARTY_LICENSES.md"],
];
for (const [file] of files) await stat(path.join(source, file));
await mkdir(destination, { recursive: true });
for (const [from, to] of files) await copyFile(path.join(source, from), path.join(destination, to));
process.stdout.write(`Built internal console: ${destination}\n`);
