/*
This file is deliberately not an app bypass. E2E tests use the same loopback
control bootstrap as the desktop launcher, then keep only short-lived cookies.
*/

import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

const repositoryRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const python = process.env.MARKETLISTENER_PYTHON || (process.platform === "win32"
  ? path.join(repositoryRoot, "desktop", ".venv", "Scripts", "python.exe")
  : path.join(repositoryRoot, "desktop", ".venv", "bin", "python"));
const output = path.join(repositoryRoot, "artifacts", "e2e-local-session.json");
const bootstrapScript = `
import json, sys, requests
from market_monitor.local_session import CONTROL_HEADER, load_control_token
port = int(sys.argv[1]); audience = sys.argv[2]
base = f"http://127.0.0.1:{port}"
session = requests.Session(); session.trust_env = False
issued = session.post(base + "/api/local-session/issue", data=b"{}", timeout=5,
    headers={CONTROL_HEADER: load_control_token(audience, port, create=False)})
issued.raise_for_status()
exchanged = session.post(base + "/api/local-session/bootstrap", timeout=5,
    headers={"Origin": base}, json={"bootstrap": issued.json()["bootstrap"]})
exchanged.raise_for_status()
name = f"ML_{audience.title()}_Session_{port}"
print(json.dumps({"name": name, "value": session.cookies[name]}))
`;

export default async function globalSetup() {
  execFileSync(python, [path.join(repositoryRoot, "desktop", "web", "e2e", "prepare-industry-fixture.py")], {
    cwd: repositoryRoot, encoding: "utf8", windowsHide: true,
  });
  const cookies = ([ [8767, "product"], [18768, "admin"] ] as const).map(([port, audience]) => {
    const cookie = JSON.parse(execFileSync(python, ["-c", bootstrapScript, String(port), audience], {
      cwd: repositoryRoot, encoding: "utf8", windowsHide: true,
    })) as { name: string; value: string };
    return { ...cookie, domain: "127.0.0.1", path: "/", expires: -1,
      httpOnly: true, secure: false, sameSite: "Strict" as const };
  });
  mkdirSync(path.dirname(output), { recursive: true });
  writeFileSync(output, JSON.stringify({ cookies, origins: [] }));
  return () => rmSync(output, { force: true });
}
