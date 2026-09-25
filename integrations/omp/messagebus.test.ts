// Tests for the omp messagebus extension. Run: bun test integrations/omp
// The live-Redis case runs only when BUS_INTEGRATION_REDIS_URL is set (CI sets it).
import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
	type BridgeDeps,
	type WaitResult,
	activate,
	formatDelivery,
	makeInjector,
	readConfig,
	runBridge,
	spawnWait,
} from "./messagebus.ts";

const REPO = resolve(import.meta.dir, "../..");

function fakeDeps(results: WaitResult[], overrides: Partial<BridgeDeps> = {}) {
	const injected: string[] = [];
	const logs: string[] = [];
	const sleeps: number[] = [];
	const controller = new AbortController();
	const deps: BridgeDeps = {
		wait: async () => {
			const next = results.shift();
			if (!next) {
				controller.abort();
				return { rc: 10, stdout: "" };
			}
			return next;
		},
		inject: text => injected.push(text),
		sleep: async ms => {
			sleeps.push(ms);
		},
		stopRequested: () => false,
		log: msg => logs.push(msg),
		...overrides,
	};
	return { deps, injected, logs, sleeps, signal: controller.signal };
}

const CONFIG = readConfig({ BUS_AGENT: "omp-1" });
if (!("agent" in CONFIG)) throw new Error("fixture config must be active");
const MSG = JSON.stringify({ id: "1-0", from: "claude-1", to: "omp-1", topic: "", reply_to: "", kind: "msg", body: "hold VM captures" });

describe("readConfig", () => {
	test("is inactive without BUS_AGENT", () => {
		expect(readConfig({})).toEqual({ inactive: "BUS_AGENT is not set" });
	});

	test("rejects an agent id outside the bus charset", () => {
		const cfg = readConfig({ BUS_AGENT: "../evil" });
		expect("inactive" in cfg && cfg.inactive).toContain("invalid agent id");
	});

	test("applies the same defaults as the shell drivers", () => {
		expect(CONFIG).toMatchObject({ agent: "omp-1", room: "main", waitSecs: 60, busBin: "bus" });
	});

	test("reads room, wait and binary overrides", () => {
		const cfg = readConfig({ BUS_AGENT: "a", BUS_ROOM: "r2", BUS_WAIT_SECS: "5", BUS_BIN: "/x/bus" });
		expect(cfg).toMatchObject({ room: "r2", waitSecs: 5, busBin: "/x/bus", busDir: "/x/.bus-state" });
	});
});

describe("formatDelivery", () => {
	test("fences message bodies as untrusted and says how to reply", () => {
		const text = formatDelivery(CONFIG, MSG);
		expect(text).toContain("----- BEGIN UNTRUSTED BUS MESSAGES -----");
		expect(text).toContain("----- END UNTRUSTED BUS MESSAGES -----");
		expect(text).toContain("hold VM captures");
		expect(text).toContain("bus --room main send --from omp-1");
		expect(text.indexOf("hold VM captures")).toBeGreaterThan(text.indexOf("BEGIN UNTRUSTED"));
	});

	test("says omp's own agent:// replies do not reach bus senders", () => {
		expect(formatDelivery(CONFIG, MSG)).toContain("agent://");
	});
});

describe("makeInjector", () => {
	type Delivered = { id: string; from: string; to: string; body: string; ts: number };
	function harness(session: { deliverIrcMessage(m: Delivered): Promise<unknown> } | undefined) {
		const asides: string[] = [];
		const logs: string[] = [];
		const inject = makeInjector({
			findMain: async () => session,
			aside: text => asides.push(text),
			log: msg => logs.push(msg),
			now: () => 42,
		});
		return { inject, asides, logs };
	}

	test("delivers as an IRC message to Main, so an active wait wakes", async () => {
		const got: Delivered[] = [];
		const { inject, asides } = harness({ deliverIrcMessage: async m => void got.push(m) });
		await inject("hello");
		expect(asides).toEqual([]);
		expect(got).toHaveLength(1);
		expect(got[0]).toMatchObject({ from: "messagebus", to: "Main", body: "hello", ts: 42 });
	});

	test("gives each delivery its own id", async () => {
		const got: Delivered[] = [];
		const { inject } = harness({ deliverIrcMessage: async m => void got.push(m) });
		await inject("a");
		await inject("b");
		expect(got[0].id).not.toBe(got[1].id);
	});

	test("falls back to an aside when omp has no Main session", async () => {
		const { inject, asides } = harness(undefined);
		await inject("hello");
		expect(asides).toEqual(["hello"]);
	});

	test("falls back to an aside and logs when IRC delivery throws", async () => {
		const { inject, asides, logs } = harness({
			deliverIrcMessage: async () => {
				throw new Error("disposed");
			},
		});
		await inject("hello");
		expect(asides).toEqual(["hello"]);
		expect(logs.join(" ")).toContain("disposed");
	});
});

describe("runBridge", () => {
	test("injects delivered messages once", async () => {
		const f = fakeDeps([{ rc: 0, stdout: MSG }]);
		await runBridge(CONFIG, f.deps, f.signal);
		expect(f.injected).toHaveLength(1);
		expect(f.injected[0]).toContain("hold VM captures");
	});

	test("a timeout waits again without injecting", async () => {
		const f = fakeDeps([{ rc: 10, stdout: "" }, { rc: 0, stdout: MSG }]);
		await runBridge(CONFIG, f.deps, f.signal);
		expect(f.injected).toHaveLength(1);
	});

	test("__SHUTDOWN__ stops the waiter and injects nothing", async () => {
		const f = fakeDeps([{ rc: 11, stdout: "" }, { rc: 0, stdout: MSG }]);
		await runBridge(CONFIG, f.deps, f.signal);
		expect(f.injected).toHaveLength(0);
		expect(f.logs.join("\n")).toContain("__SHUTDOWN__");
	});

	test("a bus error backs off, retries, and logs once per streak", async () => {
		const f = fakeDeps([
			{ rc: 3, stdout: "" },
			{ rc: 1, stdout: "" },
			{ rc: 0, stdout: MSG },
			{ rc: 3, stdout: "" },
		]);
		await runBridge(CONFIG, f.deps, f.signal);
		expect(f.sleeps).toEqual([2000, 2000, 2000]);
		expect(f.injected).toHaveLength(1);
		expect(f.logs.filter(l => l.includes("bus error"))).toHaveLength(2);
	});

	test("a stop file ends the loop before waiting", async () => {
		let waited = false;
		const f = fakeDeps([], {
			stopRequested: () => true,
			wait: async () => {
				waited = true;
				return { rc: 0, stdout: MSG };
			},
		});
		await runBridge(CONFIG, f.deps, f.signal);
		expect(waited).toBe(false);
		expect(f.logs.join("\n")).toContain("stop file");
	});

	test("an abort mid-wait injects nothing", async () => {
		const controller = new AbortController();
		const f = fakeDeps([], {
			wait: async () => {
				controller.abort();
				return { rc: 0, stdout: MSG };
			},
		});
		await runBridge(CONFIG, f.deps, controller.signal);
		expect(f.injected).toHaveLength(0);
	});
});

describe("activate", () => {
	test("stays inert in a session without UI (subagents, print mode)", () => {
		expect(activate({ hasUI: false }, { BUS_AGENT: "omp-1" })).toBe("no UI: subagent or headless session");
	});

	test("stays inert without BUS_AGENT", () => {
		expect(activate({ hasUI: true }, {})).toBe("BUS_AGENT is not set");
	});
});

describe("spawnWait", () => {
	test("returns the bus exit code and stdout, and passes the wait arguments", async () => {
		const dir = mkdtempSync(join(tmpdir(), "omp-bus-"));
		const fake = join(dir, "bus");
		writeFileSync(fake, `#!/bin/sh\necho "$@"\nexit 10\n`, { mode: 0o755 });
		const cfg = readConfig({ BUS_AGENT: "omp-1", BUS_BIN: fake, BUS_WAIT_SECS: "7" });
		if (!("agent" in cfg)) throw new Error("expected active config");
		const res = await spawnWait(cfg, new AbortController().signal);
		expect(res.rc).toBe(10);
		expect(res.stdout.trim()).toBe("--room main --json wait --as omp-1 --timeout 7");
	});

	test("a missing binary is an error result, not a throw", async () => {
		const cfg = readConfig({ BUS_AGENT: "omp-1", BUS_BIN: "/nonexistent/bus" });
		if (!("agent" in cfg)) throw new Error("expected active config");
		const res = await spawnWait(cfg, new AbortController().signal);
		expect(res.rc).not.toBe(0);
	});
});

const REDIS_URL = process.env.BUS_INTEGRATION_REDIS_URL;
describe.skipIf(!REDIS_URL)("live Redis", () => {
	test("a message sent on the real bus reaches inject()", async () => {
		const agent = `omp-it-${process.pid}`;
		const room = `omp-it-${Date.now()}`;
		const env = { ...process.env, BUS_REDIS_URL: REDIS_URL };
		const bus = join(REPO, "bus");
		const run = (args: string[]) => Bun.spawnSync([bus, "--room", room, ...args], { env });
		expect(run(["join", "--as", agent]).exitCode).toBe(0);
		expect(run(["send", "--from", "tester", "--to", agent, "live ping"]).exitCode).toBe(0);
		const cfg = readConfig({ BUS_AGENT: agent, BUS_ROOM: room, BUS_BIN: bus, BUS_WAIT_SECS: "5" });
		if (!("agent" in cfg)) throw new Error("expected active config");
		const injected: string[] = [];
		const controller = new AbortController();
		await runBridge(
			cfg,
			{
				wait: signal => spawnWait(cfg, signal, env),
				inject: text => {
					injected.push(text);
					controller.abort();
				},
				sleep: async () => {},
				stopRequested: () => false,
				log: () => {},
			},
			controller.signal,
		);
		expect(injected).toHaveLength(1);
		expect(injected[0]).toContain("live ping");
	});
});
