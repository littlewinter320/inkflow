import { spawn } from "node:child_process";
import { createServer } from "node:net";

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
