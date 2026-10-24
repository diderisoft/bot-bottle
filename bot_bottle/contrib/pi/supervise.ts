/**
 * bot-bottle supervise tools for pi.
 *
 * pi has no MCP client, so this extension stands in for one: at startup it
 * lists the bottle's supervise tools (egress-allow and friends) and
 * registers each as a pi tool that forwards to the supervise server over
 * MCP's JSON-RPC. The provider writes the server's URL and this bottle's
 * identity token to ~/.pi/agent/bot-bottle-supervise.json.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface Config {
	url: string;
	header: string;
	token: string;
}

interface McpTool {
	name: string;
	description?: string;
	inputSchema?: Record<string, unknown>;
}

interface McpContent {
	type: string;
	text?: string;
}

function readConfig(): Config | null {
	try {
		return JSON.parse(
			readFileSync(join(homedir(), ".pi", "agent", "bot-bottle-supervise.json"), "utf8"),
		) as Config;
	} catch {
		return null;
	}
}

async function rpc(config: Config, method: string, params: object, signal?: AbortSignal) {
	const headers: Record<string, string> = { "Content-Type": "application/json" };
	if (config.token) headers[config.header] = config.token;
	const resp = await fetch(config.url, {
		method: "POST",
		headers,
		body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method, params }),
		signal,
	});
	const body = (await resp.json()) as { result?: any; error?: { message?: string } };
	if (body.error) throw new Error(body.error.message || `supervise ${method} failed`);
	return body.result;
}

export default async function (pi: ExtensionAPI) {
	const config = readConfig();
	if (!config) return;
	let tools: McpTool[];
	try {
		await rpc(config, "initialize", { protocolVersion: "2025-06-18", capabilities: {},
			clientInfo: { name: "pi", version: "bot-bottle" } });
		tools = ((await rpc(config, "tools/list", {})) ?? {}).tools ?? [];
	} catch {
		return; // supervise unreachable: pi still runs, without the tools
	}
	for (const tool of tools) {
		pi.registerTool({
			name: tool.name,
			label: `bot-bottle ${tool.name}`,
			description: tool.description ?? "",
			parameters: (tool.inputSchema ?? { type: "object", properties: {} }) as any,
			async execute(_toolCallId, params, signal) {
				const result = await rpc(config, "tools/call",
					{ name: tool.name, arguments: params ?? {} }, signal);
				const content = ((result?.content ?? []) as McpContent[])
					.filter((c) => c.type === "text")
					.map((c) => ({ type: "text" as const, text: c.text ?? "" }));
				if (result?.isError) throw new Error(content.map((c) => c.text).join("\n"));
				return { content, details: {} };
			},
		});
	}
}
