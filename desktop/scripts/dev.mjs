import { spawn } from "node:child_process";
import { createServer } from "node:net";

// Compile before Electron can see a previous main.js after a source update.
await new Promise((resolve, reject) => {
  const compile = spawn(process.execPath, ["node_modules/typescript/bin/tsc", "-p", "tsconfig.electron.json"], {
    cwd: new URL("..", import.meta.url), stdio: "inherit", windowsHide: true,
  });
  compile.once("error", reject);
  compile.once("exit", code => code === 0 ? resolve() : reject(new Error(`桌面启动编译失败（${code}）`)));
});

// Give each development launch its own Vite port, including when 5173 belongs
// to another application or an older InkFlow frontend.
const server = createServer();
const port = await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    server.close(() => resolve(address.port));
  });
});

const child = spawn(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", "npm run dev:services"], {
  cwd: new URL("..", import.meta.url),
  env: { ...process.env, INKFLOW_DEV_PORT: String(port) },
  stdio: "inherit",
  windowsHide: true,
});
child.once("error", (error) => { console.error(error); process.exitCode = 1; });
child.once("exit", (code) => { process.exitCode = code ?? 1; });
