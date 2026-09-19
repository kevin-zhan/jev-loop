import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));

function rpcClient(home) {
  const child = spawn("pi", ["--mode", "rpc", "--no-session", "-e", root], {
    env: { ...process.env, JEV_LOOP_HOME: home },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const events = [];
  const waiters = [];
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
  child.stdout.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) break;
      let line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (!line) continue;
      const event = JSON.parse(line);
      events.push(event);
      for (const waiter of [...waiters]) {
        if (waiter.predicate(event)) {
          clearTimeout(waiter.timer);
          waiters.splice(waiters.indexOf(waiter), 1);
          waiter.resolve(event);
        }
      }
    }
  });
  function waitFor(predicate, timeoutMs = 12_000) {
    const existing = events.find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolvePromise, reject) => {
      const waiter = {
        predicate,
        resolve: resolvePromise,
        timer: setTimeout(() => reject(new Error(`RPC timeout; stderr=${stderr}`)), timeoutMs),
      };
      waiters.push(waiter);
    });
  }
  function send(value) {
    child.stdin.write(`${JSON.stringify(value)}\n`);
  }
  return { child, events, send, waitFor, stderr: () => stderr };
}

test("pi loads the package and its command reaches the Python host without a model call", async () => {
  const home = await mkdtemp(resolve(tmpdir(), "pi-jev-extension-"));
  const rpc = rpcClient(home);
  try {
    rpc.send({ id: "commands", type: "get_commands" });
    const commands = await rpc.waitFor((event) => event.type === "response" && event.id === "commands");
    assert.equal(commands.success, true);
    const names = commands.data.commands.map((command) => command.name);
    assert.ok(names.includes("jev-runs"));
    assert.ok(names.includes("jev-self-test"));
    assert.ok(names.includes("jev-stop-all"));
    assert.ok(names.includes("skill:pi-jev"));

    rpc.send({ id: "runs", type: "prompt", message: "/jev-runs" });
    const accepted = await rpc.waitFor((event) => event.type === "response" && event.id === "runs");
    const notice = await rpc.waitFor(
      (event) => event.type === "extension_ui_request" && event.method === "notify" && event.message.includes("Jev Loop"),
    );
    assert.equal(accepted.success, true);
    assert.match(notice.message, /No Jev Loop runs/);

    rpc.send({ id: "self-test", type: "prompt", message: "/jev-self-test" });
    const selfTestAccepted = await rpc.waitFor((event) => event.type === "response" && event.id === "self-test");
    const selfTestNotice = await rpc.waitFor(
      (event) => event.type === "extension_ui_request" && event.method === "notify" && event.message.includes("self-test PASS"),
      20_000,
    );
    assert.equal(selfTestAccepted.success, true);
    assert.match(selfTestNotice.message, /world ticks/);
    assert.match(selfTestNotice.message, /inputs released/);
    assert.equal(rpc.events.some((event) => event.type === "extension_error"), false);
  } finally {
    rpc.child.kill("SIGTERM");
    await new Promise((resolvePromise) => rpc.child.once("close", resolvePromise));
    await rm(home, { recursive: true, force: true });
  }
});
