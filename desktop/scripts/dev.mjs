import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync } from "node:fs";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
let executable = require("electron");
if (process.platform === "win32") {
  const version = require("../package.json").version;
  const icon = path.resolve("resources/icon.ico");
  const identity = `${require("electron/package.json").version}-${version}-${createHash("sha256").update(readFileSync(icon)).digest("hex").slice(0, 12)}`;
  const cache = "D:/墨流/cache/desktop-runtime";
  const runtime = path.join(cache, identity);
  executable = path.join(runtime, "墨流 InkFlow.exe");
  if (!existsSync(executable)) {
    mkdirSync(cache, { recursive: true });
    const prepared = mkdtempSync(path.join(cache, "prepare-"));
    cpSync(path.dirname(require("electron")), prepared, { recursive: true });
    const branded = path.join(prepared, "墨流 InkFlow.exe");
    copyFileSync(icon, path.join(prepared, "icon.ico"));
    await new Promise((resolve, reject) => {
      // The bundled resource editor needs relative ASCII file paths on Windows.
      const edit = spawn(require.resolve("electron-winstaller/vendor/rcedit.exe"), ["electron.exe",
        "--set-version-string", "ProductName", "墨流 InkFlow",
        "--set-version-string", "FileDescription", "墨流 InkFlow",
        "--set-version-string", "OriginalFilename", "墨流 InkFlow.exe",
        "--set-file-version", version, "--set-product-version", version, "--set-icon", "icon.ico",
      ], { cwd: prepared, stdio: "inherit", windowsHide: true });
      edit.once("error", reject);
      edit.once("exit", code => code === 0 ? resolve() : reject(new Error(`墨流开发进程信息写入失败（${code}）`)));
    });
    renameSync(path.join(prepared, "electron.exe"), branded);
    renameSync(prepared, runtime);
  }
}

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
  env: { ...process.env,
    PATH: `${path.dirname(process.execPath)}${path.delimiter}${path.dirname(process.env.ComSpec || "C:/Windows/System32/cmd.exe")}${path.delimiter}${process.env.PATH || process.env.Path || ""}`,
    INKFLOW_DEV_PORT: String(port), INKFLOW_DEV_EXECUTABLE: executable },
  stdio: "inherit",
  windowsHide: true,
});
child.once("error", (error) => { console.error(error); process.exitCode = 1; });
child.once("exit", (code) => { process.exitCode = code ?? 1; });
