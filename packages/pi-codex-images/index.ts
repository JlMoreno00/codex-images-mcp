import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { Type } from "typebox";
import { materializeArguments, persistArtifacts, resolveToken, validateConfig, type EdgeConfig } from "./core.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = process.env.PI_CODEX_IMAGES_CONFIG ?? resolve(HERE, "config.json");

async function loadConfig(): Promise<ReturnType<typeof validateConfig>> {
  const raw = JSON.parse(await readFile(CONFIG_PATH, "utf8")) as EdgeConfig;
  return validateConfig(raw);
}

const MAX_TOOL_SUMMARY_LENGTH = 500;

type RemoteToolResult = {
  isError?: unknown;
  content?: unknown;
  structuredContent?: unknown;
};

function toolTextSummary(content: unknown): string {
  const summary = Array.isArray(content)
    ? content
      .filter((part): part is { type: "text"; text: string } => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text.trim())
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
    : "";
  return summary.length > MAX_TOOL_SUMMARY_LENGTH
    ? `${summary.slice(0, MAX_TOOL_SUMMARY_LENGTH - 1)}…`
    : summary;
}

function throwIfRemoteToolError(result: RemoteToolResult): void {
  if (!result.isError) return;
  const summary = toolTextSummary(result.content) || "Remote MCP tool reported an error without a text summary.";
  throw new Error(`REMOTE_TOOL_ERROR: ${summary}`);
}

export function mapToolResult(result: RemoteToolResult, materialization: unknown) {
  throwIfRemoteToolError(result);
  return {
    content: [{ type: "text" as const, text: toolTextSummary(result.content) || "Remote MCP tool completed successfully." }],
    details: { structuredContent: result.structuredContent, materialization },
  };
}

export default function (pi: any): void {
  pi.registerTool({
    name: "codex_images",
    label: "Codex Images",
    description: "Generate or edit images through a codex-images-mcp server (transport v2). Securely materializes authorized local references and instruction files, retrieves resource artifacts, verifies integrity and dimensions, and writes them atomically to client_destination.",
    promptSnippet: "Generate or edit images with secure local-file and artifact materialization.",
    parameters: Type.Object({
      prompt: Type.Any({ description: "Prompt string, structured spec, or array of prompts." }),
      mode: Type.Optional(Type.String()),
      batch_mode: Type.Optional(Type.String()),
      count: Type.Optional(Type.Number()),
      references: Type.Optional(Type.Array(Type.Any())),
      instruction_files: Type.Optional(Type.Array(Type.Any())),
      skills: Type.Optional(Type.Array(Type.String())),
      mask: Type.Optional(Type.String()),
      output: Type.Optional(Type.Any()),
      client_destination: Type.Optional(Type.Object({
        type: Type.Optional(Type.Literal("local_directory")),
        path: Type.Optional(Type.String()),
        directory: Type.Optional(Type.String()),
        filename: Type.Optional(Type.String()),
        overwrite: Type.Optional(Type.Boolean()),
      }, { additionalProperties: false })),
    }, { additionalProperties: true }),
    async execute(_toolCallId: string, params: Record<string, any>, signal?: AbortSignal) {
      const config = await loadConfig();
      const { wire, destination } = await materializeArguments(params, config);
      const token = await resolveToken(config);
      const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
      const transport = new StreamableHTTPClientTransport(new URL(config.endpoint), {
        requestInit: headers ? { headers } : undefined,
      });
      const client = new Client({ name: "pi-codex-images", version: "0.1.0" });
      try {
        await client.connect(transport);
        const result: any = await client.callTool(
          { name: "codex_images", arguments: wire },
          { signal, timeout: config.requestTimeoutMs },
        );
        throwIfRemoteToolError(result);
        const structured = result.structuredContent;
        let materialization: any = undefined;
        if (destination) {
          if (!structured || !Array.isArray(structured.artifacts)) throw new Error("ARTIFACT_INCOMPLETE: Tool result has no structured artifact manifest");
          const files = await persistArtifacts(
            structured,
            destination,
            async (uri) => {
              const resource: any = await client.readResource({ uri }, { signal, timeout: config.requestTimeoutMs });
              const item = resource.contents?.[0];
              if (!item || typeof item.blob !== "string") throw new Error("ARTIFACT_READ_FAILED: Resource did not return one binary blob");
              return { blob: item.blob, mimeType: item.mimeType };
            },
            config,
          );
          materialization = { status: "completed", files };
        }
        const summary = (result.content ?? []).filter((part: any) => part?.type === "text").map((part: any) => part.text).join("\n");
        return mapToolResult({ ...result, content: summary ? [{ type: "text", text: summary }] : [] }, materialization);
      } catch (error: unknown) {
        throw error instanceof Error ? error : new Error(String(error));
      } finally {
        await client.close().catch(() => undefined);
      }
    },
  });
}
