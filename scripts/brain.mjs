import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

const root = process.cwd();
const localPython = path.join(root, "venv", "Scripts", "python.exe");
const python = process.platform === "win32" && existsSync(localPython) ? localPython : "python";
const port = process.env.BRAIN_PORT || "8100";
const url = `http://127.0.0.1:${port}/ai-context`;
const server = spawn(python, ["-m", "uvicorn", "brain_app:app", "--host", "127.0.0.1", "--port", port], {
  cwd: root,
  stdio: "inherit",
});

function openBrowser() {
  if (process.platform === "win32") {
    spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
  } else if (process.platform === "darwin") {
    spawn("open", [url], { detached: true, stdio: "ignore" }).unref();
  } else {
    spawn("xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
  }
}

setTimeout(openBrowser, 900);
server.on("exit", (code) => process.exit(code ?? 0));
process.on("SIGINT", () => server.kill("SIGINT"));
process.on("SIGTERM", () => server.kill("SIGTERM"));
