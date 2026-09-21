import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));

// The test runs pi with an isolated agent/config directory: a developer's own pi settings may
// already register another copy of this package, which would otherwise abort startup with a tool
// conflict and leave the RPC client waiting. `PI_OFFLINE` keeps pi's own startup checks away from
// the network. Nothing in the user's pi configuration is read or written.
function rpcClient(home, agentDir) {
  const child = spawn("pi", ["--mode", "rpc", "--no-session", "-e", root], {
    env: {
      ...process.env,
      JEV_LOOP_HOME: home,
      PI_CODING_AGENT_DIR: agentDir,
      PI_OFFLINE: "1",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const events = [];
  const waiters = [];
  let buffer = "";
  let stderr = "";
  let exited = null;
  let spawnError = null;

  const diagnostic = () => {
    const text = stderr.trim().replace(/\s+/g, " ");
    return text.length > 2000 ? `${text.slice(0, 2000)}…` : text;
  };
  const exitMessage = () =>
    `pi exited before answering (code=${exited?.code}, signal=${exited?.signal}); stderr=${diagnostic()}`;
  const failWaiters = (error) => {
    for (const waiter of [...waiters]) {
      clearTimeout(waiter.timer);
      waiters.splice(waiters.indexOf(waiter), 1);
      waiter.reject(error);
    }
  };

  child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
  child.on("error", (error) => {
    spawnError = error;
    failWaiters(new Error(`failed to start pi: ${error.message}`));
  });
  child.on("exit", (code, signal) => {
    exited = { code, signal };
    failWaiters(new Error(exitMessage()));
  });
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
    if (spawnError) return Promise.reject(new Error(`failed to start pi: ${spawnError.message}`));
    if (exited) return Promise.reject(new Error(exitMessage()));
    const existing = events.find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolvePromise, reject) => {
      const waiter = {
        predicate,
        resolve: resolvePromise,
        reject,
        timer: setTimeout(() => {
          waiters.splice(waiters.indexOf(waiter), 1);
          reject(new Error(`RPC timeout; stderr=${diagnostic()}`));
        }, timeoutMs),
      };
      waiters.push(waiter);
    });
  }
  function send(value) {
    child.stdin.write(`${JSON.stringify(value)}\n`);
  }
  return { child, events, send, waitFor, stderr: () => stderr };
}

function closeChild(child) {
  return new Promise((resolvePromise) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolvePromise();
      return;
    }
    const fallback = setTimeout(() => {
      child.kill("SIGKILL");
      resolvePromise();
    }, 5_000);
    child.once("close", () => {
      clearTimeout(fallback);
      resolvePromise();
    });
    child.kill("SIGTERM");
  });
}

test("pi loads the package and its command reaches the Python host without a model call", { timeout: 90_000 }, async () => {
  const home = await mkdtemp(resolve(tmpdir(), "pi-jev-extension-home-"));
  const agentDir = await mkdtemp(resolve(tmpdir(), "pi-jev-extension-agent-"));
  await mkdir(agentDir, { recursive: true });
  const rpc = rpcClient(home, agentDir);
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
    await closeChild(rpc.child);
    await rm(home, { recursive: true, force: true });
    await rm(agentDir, { recursive: true, force: true });
  }
});
