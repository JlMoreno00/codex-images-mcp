import assert from "node:assert/strict";
import { lstat, mkdtemp, readFile, realpath, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { mapToolResult } from "../index.ts";
import { materializeArguments, parseImageDimensions, persistArtifacts, validateConfig } from "../core.ts";

const PNG_1X1 = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+XvqaAAAAAElFTkSuQmCC", "base64");

function jpegHeader(width: number, height: number): Buffer {
  return Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x0b, 0x08, height >> 8, height & 0xff, width >> 8, width & 0xff, 0x01, 0x01, 0x11, 0x00]);
}
function webpHeader(kind: "VP8 " | "VP8L" | "VP8X", width: number, height: number): Buffer {
  const length = kind === "VP8L" ? 25 : 30;
  const data = Buffer.alloc(length);
  data.write("RIFF", 0); data.writeUInt32LE(length - 8, 4); data.write("WEBP", 8); data.write(kind, 12);
  if (kind === "VP8 ") {
    data.writeUInt32LE(10, 16); data.set([0x9d, 0x01, 0x2a], 23); data.writeUInt16LE(width, 26); data.writeUInt16LE(height, 28);
  } else if (kind === "VP8L") {
    const packedWidth = width - 1; const packedHeight = height - 1;
    data.writeUInt32LE(5, 16); data.set([0x2f, packedWidth & 0xff, ((packedWidth >> 8) & 0x3f) | ((packedHeight & 0x03) << 6), (packedHeight >> 2) & 0xff, (packedHeight >> 10) & 0x0f], 20);
  } else {
    data.writeUInt32LE(10, 16); data.writeUIntLE(width - 1, 24, 3); data.writeUIntLE(height - 1, 27, 3);
  }
  return data;
}

test("parses only PNG, JPEG, and WebP dimension headers", () => {
  assert.deepEqual(parseImageDimensions(PNG_1X1, "image/png"), { width: 1, height: 1 });
  assert.deepEqual(parseImageDimensions(jpegHeader(320, 240), "image/jpeg"), { width: 320, height: 240 });
  assert.deepEqual(parseImageDimensions(webpHeader("VP8 ", 320, 240), "image/webp"), { width: 320, height: 240 });
  assert.deepEqual(parseImageDimensions(webpHeader("VP8L", 300, 200), "image/webp"), { width: 300, height: 200 });
  assert.deepEqual(parseImageDimensions(webpHeader("VP8X", 320, 240), "image/webp"), { width: 320, height: 240 });
});

test("parses JPEG dimensions after a valid short DRI segment", () => {
  const jpegWithDri = Buffer.concat([
    Buffer.from([0xff, 0xd8, 0xff, 0xdd, 0x00, 0x04, 0x00, 0x00]),
    jpegHeader(320, 240).subarray(2),
  ]);
  assert.deepEqual(parseImageDimensions(jpegWithDri, "image/jpeg"), { width: 320, height: 240 });
});

test("rejects malformed, unsupported, and MIME/signature-mismatched image headers", () => {
  assert.throws(() => parseImageDimensions(PNG_1X1, "image/jpeg"), /signature/i);
  assert.throws(() => parseImageDimensions(PNG_1X1.subarray(0, 8), "image/png"), /truncated/i);
  assert.throws(() => parseImageDimensions(Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x02]), "image/jpeg"), /SOF segment is malformed or truncated/i);
  assert.throws(() => parseImageDimensions(webpHeader("VP8 ", 1, 1).subarray(0, 29), "image/webp"), /truncated|malformed/i);
  assert.throws(() => parseImageDimensions(Buffer.from("GIF89a"), "image/gif"), /unsupported/i);
});

test("local files become inline blobs and client_destination never crosses wire", async () => {
  const root = await mkdtemp(join(tmpdir(), "codex-images-input-"));
  const image = join(root, "ref.png");
  await writeFile(image, PNG_1X1);
  const { wire, destination } = await materializeArguments({
    prompt: "x", references: [{ source: { type: "local_file", path: image }, role: "style" }],
    client_destination: { directory: root, filename: "out.png" },
  }, { allowedInputRoots: [root], allowedOutputRoots: [root], maxFileBytes: 1024, maxRequestBytes: 2048 });
  assert.equal("client_destination" in wire, false);
  assert.equal((wire.references as any[])[0].source.type, "inline_blob");
  assert.equal((wire.references as any[])[0].source.data, PNG_1X1.toString("base64"));
  assert.equal(destination?.filename, "out.png");
  assert.equal(JSON.stringify(wire).includes(image), false);
});

test("rejects symlink escapes, non-regular files, MIME/signature mismatches, and request bounds", async () => {
  const root = await mkdtemp(join(tmpdir(), "codex-images-root-"));
  await symlink("/etc/passwd", join(root, "escape.png"));
  await writeFile(join(root, "mismatch.jpg"), PNG_1X1);
  await assert.rejects(() => materializeArguments({ prompt: "x", references: [join(root, "escape.png")] }, { allowedInputRoots: [root], allowedOutputRoots: [root], maxFileBytes: 1e6, maxRequestBytes: 1e6 }), /allowed input roots/);
  await assert.rejects(() => materializeArguments({ prompt: "x", references: [root] }, { allowedInputRoots: [root], allowedOutputRoots: [root], maxFileBytes: 1e6, maxRequestBytes: 1e6 }), /regular file/);
  await assert.rejects(() => materializeArguments({ prompt: "x", references: [join(root, "mismatch.jpg")] }, { allowedInputRoots: [root], allowedOutputRoots: [root], maxFileBytes: 1e6, maxRequestBytes: 1e6 }), /MIME|signature/i);
});

test("resource recovery verifies hash, MIME, dimensions and atomically writes", async () => {
  const root = await mkdtemp(join(tmpdir(), "codex-images-output-"));
  const artifact = { index: 0, status: "completed", uri: "mcp://codex-images/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/artifacts/0", name: "x.png", mime_type: "image/png", bytes: PNG_1X1.length, width: 1, height: 1, sha256: await import("node:crypto").then(({createHash}) => createHash("sha256").update(PNG_1X1).digest("hex")) };
  const paths = await persistArtifacts({ artifacts: [artifact] }, { directory: root, filename: "final.png" }, async () => ({ blob: PNG_1X1.toString("base64"), mimeType: "image/png" }), { allowedOutputRoots: [root], maxFileBytes: 1024 });
  assert.deepEqual(paths, [join(await realpath(root), "final.png")]);
  assert.deepEqual(await readFile(paths[0]), PNG_1X1);
  await assert.rejects(() => persistArtifacts({ artifacts: [{ ...artifact, sha256: "0".repeat(64) }] }, { directory: root, filename: "bad.png" }, async () => ({ blob: PNG_1X1.toString("base64"), mimeType: "image/png" }), { allowedOutputRoots: [root], maxFileBytes: 1024 }), /sha256/i);
});

test("rejects unauthorized and symlink-escaping destinations before creating them", async () => {
  const allowed = await mkdtemp(join(tmpdir(), "codex-images-allowed-output-"));
  const rejected = join(await mkdtemp(join(tmpdir(), "codex-images-unallowed-parent-")), "new-output");
  await assert.rejects(() => persistArtifacts({ artifacts: [] }, { directory: rejected }, async () => { throw new Error("must not read artifact"); }, { allowedOutputRoots: [allowed], maxFileBytes: 1024 }), /outside allowed output roots/);
  await assert.rejects(() => lstat(rejected), { code: "ENOENT" });

  const escapedRoot = await mkdtemp(join(tmpdir(), "codex-images-escaped-output-"));
  const escaped = join(escapedRoot, "new-output");
  await symlink(escapedRoot, join(allowed, "escape"));
  await assert.rejects(() => persistArtifacts({ artifacts: [] }, { directory: join(allowed, "escape", "new-output") }, async () => { throw new Error("must not read artifact"); }, { allowedOutputRoots: [allowed], maxFileBytes: 1024 }), /outside allowed output roots/);
  await assert.rejects(() => lstat(escaped), { code: "ENOENT" });
});

test("configuration requires explicit roots and protects remote auth", () => {
  assert.throws(() => validateConfig({ endpoint: "https://example.com/mcp", allowedInputRoots: [], allowedOutputRoots: [] } as any), /allowedInputRoots/);
  assert.throws(() => validateConfig({ endpoint: "http://example.com/mcp", allowedInputRoots: ["/tmp"], allowedOutputRoots: ["/tmp"] } as any), /HTTPS/);
});

test("maps successful MCP results to compact Pi content and persistent details", () => {
  const structuredContent = { artifacts: [{ uri: "mcp://example/artifact/0" }] };
  const materialization = { status: "completed", files: ["/output/image.png"] };
  const mapped = mapToolResult({
    content: [{ type: "text", text: "Generated 1 image" }, { type: "image", data: "ignored" }],
    structuredContent,
  }, materialization);

  assert.deepEqual(mapped, {
    content: [{ type: "text", text: "Generated 1 image" }],
    details: { structuredContent, materialization },
  });
  assert.equal(mapped.content[0].text.includes("artifacts"), false);
});

test("throws bounded actionable errors for failed MCP results", () => {
  assert.throws(
    () => mapToolResult({ isError: true, content: [{ type: "text", text: "Remote policy rejected this prompt" }] }, undefined),
    /REMOTE_TOOL_ERROR: Remote policy rejected this prompt/,
  );
  assert.throws(
    () => mapToolResult({ isError: true, content: [] }, undefined),
    /REMOTE_TOOL_ERROR: Remote MCP tool reported an error without a text summary\./,
  );
  assert.throws(
    () => mapToolResult({ isError: true, content: [{ type: "text", text: "x".repeat(600) }] }, undefined),
    (error: Error) => error.message.length <= 520 && error.message.endsWith("…"),
  );
});
