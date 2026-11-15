import { spawn } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { cp } from "node:fs/promises";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));

/** 首次只复制一次带品牌的运行时，后续启动直接复用。 */
async function developmentExecutable() {
  const electron = require("electron");
  if (process.platform !== "win32") return electron;
  const version = require("../package.json").version;
  const icon = path.join(desktopRoot, "resources/icon.ico");
  const identity = `${require("electron/package.json").version}-${version}-${createHash("sha256").update(readFileSync(icon)).digest("hex").slice(0, 12)}`;
  const cache = "D:/墨流/cache/desktop-runtime";
  const runtime = path.join(cache, identity);
  const executable = path.join(runtime, "墨流 InkFlow.exe");
  if (!existsSync(executable)) {
    mkdirSync(cache, { recursive: true });
    const prepared = mkdtempSync(path.join(cache, "prepare-"));
    await cp(path.dirname(electron), prepared, { recursive: true });
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
  return executable;
}

/** 校验源码与产物的内容；切分支、手动构建或删除产物后都不会启动旧主进程。 */
async function prepareMainProcess() {
  const started = performance.now();
  const sources = readdirSync(path.join(desktopRoot, "electron"), { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile() && entry.name.endsWith(".ts"))
    .map(entry => path.join(entry.parentPath, entry.name)).sort();
  const inputs = [path.join(desktopRoot, "tsconfig.electron.json"), path.join(desktopRoot, "package-lock.json"), ...sources];
  const outputs = sources.filter(source => !source.endsWith(".d.ts")).map(source =>
    path.join(desktopRoot, "dist-electron", path.relative(path.join(desktopRoot, "electron"), source).replace(/\.ts$/, ".js")));
  const stamp = path.join(desktopRoot, "build", "dev-electron.hash");
  const fingerprint = (files) => {
    const hash = createHash("sha256");
    for (const file of files) {
      const content = readFileSync(file);
      hash.update(`${file}:${content.length}\n`).update(content);
    }
    return hash.digest("hex");
  };
  if (outputs.length && existsSync(stamp) && outputs.every(file => existsSync(file))
      && readFileSync(stamp, "utf8") === fingerprint([...inputs, ...outputs])) {
    console.info(`墨流主进程复用已编译文件（${Math.round(performance.now() - started)} ms）`);
    return;
  }
  const sourceHash = fingerprint(inputs);
  await new Promise((resolve, reject) => {
    const compile = spawn(process.execPath, [require.resolve("typescript/bin/tsc"), "-p", "tsconfig.electron.json", "--noEmitOnError"], {
      cwd: desktopRoot, stdio: "inherit", windowsHide: true,
    });
    compile.once("error", reject);
    compile.once("exit", code => code === 0 ? resolve() : reject(new Error(`桌面启动编译失败（${code}）`)));
  });
  if (sourceHash !== fingerprint(inputs)) throw new Error("桌面源码在启动编译时发生变化，请重新打开墨流。");
  mkdirSync(path.dirname(stamp), { recursive: true });
  writeFileSync(stamp, fingerprint([...inputs, ...outputs]));
  console.info(`墨流主进程编译完成（${Math.round(performance.now() - started)} ms）`);
}

/** 直接等待 Vite 自己的监听结果，避免多层命令启动和端口轮询。 */
async function startFrontend() {
  const started = performance.now();
  const { createServer } = await import("vite");
  const server = await createServer({ root: desktopRoot, server: { host: "localhost", port: 0 } });
  try {
    await server.listen();
    console.info(`墨流开发页面服务已就绪（${Math.round(performance.now() - started)} ms）`);
    return server;
  } catch (cause) {
    await server.close();
    throw cause;
  }
}

const frontend = startFrontend();
try {
  const [server, executable] = await Promise.all([frontend, developmentExecutable(), prepareMainProcess()]);
  const url = server.resolvedUrls?.local[0];
  if (!url) throw new Error("墨流开发页面服务尚未取得本机地址。");
  const child = spawn(executable, ["."], {
    cwd: desktopRoot, env: { ...process.env, VITE_DEV_SERVER_URL: url }, stdio: "inherit", windowsHide: true,
  });
  process.exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", code => resolve(code ?? 1));
  });
} finally {
  const server = await frontend.catch(() => null);
  await server?.close();
}
