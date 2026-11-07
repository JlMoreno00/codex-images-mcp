import { createHash, randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { access, lstat, mkdir, open, readFile, realpath, rename, stat, unlink } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, extname, isAbsolute, join, resolve, sep } from "node:path";

export type EdgeConfig = {
  endpoint: string;
  allowedInputRoots: string[];
  allowedOutputRoots: string[];
  maxFileBytes?: number;
  maxRequestBytes?: number;
  requestTimeoutMs?: number;
  tokenEnv?: string;
  tokenFile?: string;
};
export type Destination = { type?: "local_directory"; path?: string; directory?: string; filename?: string; overwrite?: boolean };
type Limits = Pick<EdgeConfig, "allowedInputRoots" | "allowedOutputRoots"> & { maxFileBytes: number; maxRequestBytes?: number };
const MIB = 1024 * 1024;
const MIME: Record<string, string> = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".md": "text/markdown", ".txt": "text/plain" };
const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);
const MAX_IMAGE_HEADER_BYTES = 256 * 1024;
const MAX_JPEG_SEGMENTS = 1024;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function inside(path: string, root: string): boolean { return path === root || path.startsWith(root.endsWith(sep) ? root : root + sep); }
function imageError(message: string): never { throw new Error(message); }
function dimensions(width: number, height: number): { width: number; height: number } {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) imageError("Image dimensions must be non-zero");
  return { width, height };
}
function matches(data: Buffer, offset: number, bytes: ArrayLike<number>): boolean {
  for (let index = 0; index < bytes.length; index++) if (data[offset + index] !== bytes[index]) return false;
  return true;
}

/** Parses only the bounded dimension headers needed by the codex-images artifact contract. */
export function parseImageDimensions(data: Buffer, mime: string): { width: number; height: number } {
  if (!IMAGE_MIMES.has(mime)) imageError("Unsupported image MIME type");
  const limit = Math.min(data.length, MAX_IMAGE_HEADER_BYTES);

  if (mime === "image/png") {
    if (!matches(data, 0, PNG_SIGNATURE)) imageError("Image MIME type does not match binary signature");
    if (limit < 24) imageError("PNG dimensions are truncated");
    if (data.readUInt32BE(8) !== 13 || !matches(data, 12, [0x49, 0x48, 0x44, 0x52])) imageError("PNG IHDR is malformed");
    return dimensions(data.readUInt32BE(16), data.readUInt32BE(20));
  }

  if (mime === "image/jpeg") {
    if (!matches(data, 0, [0xff, 0xd8])) imageError("Image MIME type does not match binary signature");
    let offset = 2;
    let segments = 0;
    while (offset < limit && segments++ < MAX_JPEG_SEGMENTS) {
      if (data[offset++] !== 0xff) imageError("JPEG marker stream is malformed");
      while (offset < limit && data[offset] === 0xff) offset++;
      if (offset >= limit) imageError("JPEG marker is truncated");
      const marker = data[offset++];
      if (marker === 0xd9 || marker === 0xda) imageError("JPEG dimensions are missing");
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
      if (offset + 2 > limit) imageError("JPEG segment is truncated");
      const length = data.readUInt16BE(offset);
      if (length < 2 || offset + length > limit) imageError("JPEG segment is malformed or truncated");
      if ((marker >= 0xc0 && marker <= 0xc3) || (marker >= 0xc5 && marker <= 0xc7) || (marker >= 0xc9 && marker <= 0xcb) || (marker >= 0xcd && marker <= 0xcf)) {
        if (length < 8) imageError("JPEG SOF segment is malformed or truncated");
        return dimensions(data.readUInt16BE(offset + 5), data.readUInt16BE(offset + 3));
      }
      offset += length;
    }
    imageError(segments > MAX_JPEG_SEGMENTS || data.length > limit ? "JPEG dimensions exceed the bounded header scan" : "JPEG dimensions are missing");
  }

  if (!matches(data, 0, [0x52, 0x49, 0x46, 0x46]) || !matches(data, 8, [0x57, 0x45, 0x42, 0x50])) imageError("Image MIME type does not match binary signature");
  if (limit < 20 || data.readUInt32LE(4) + 8 > data.length) imageError("WebP header is truncated");
  const chunkSize = data.readUInt32LE(16);
  if (20 + chunkSize > data.length) imageError("WebP dimension chunk is truncated");
  if (matches(data, 12, [0x56, 0x50, 0x38, 0x20])) {
    if (chunkSize < 10 || limit < 30 || !matches(data, 23, [0x9d, 0x01, 0x2a])) imageError("WebP VP8 dimension header is malformed");
    return dimensions(data.readUInt16LE(26) & 0x3fff, data.readUInt16LE(28) & 0x3fff);
  }
  if (matches(data, 12, [0x56, 0x50, 0x38, 0x4c])) {
    if (chunkSize < 5 || limit < 25 || data[20] !== 0x2f) imageError("WebP VP8L dimension header is malformed");
    return dimensions(1 + data[21] + ((data[22] & 0x3f) << 8), 1 + (data[22] >> 6) + (data[23] << 2) + ((data[24] & 0x0f) << 10));
  }
  if (matches(data, 12, [0x56, 0x50, 0x38, 0x58])) {
    if (chunkSize !== 10 || limit < 30) imageError("WebP VP8X dimension header is malformed");
    return dimensions(1 + data.readUIntLE(24, 3), 1 + data.readUIntLE(27, 3));
  }
  imageError("Unsupported WebP image encoding");
}
async function roots(values: string[]): Promise<string[]> { return Promise.all(values.map(value => realpath(resolve(value)))); }
function typed(code: string, message: string): Error { const error = new Error(`${code}: ${message}`); (error as any).code = code; return error; }

export function validateConfig(config: EdgeConfig): Required<Omit<EdgeConfig,"tokenEnv"|"tokenFile">> & Pick<EdgeConfig,"tokenEnv"|"tokenFile"> {
  if (!Array.isArray(config.allowedInputRoots) || config.allowedInputRoots.length === 0) throw typed("CONFIG_INVALID", "allowedInputRoots must be explicit and non-empty");
  if (!Array.isArray(config.allowedOutputRoots) || config.allowedOutputRoots.length === 0) throw typed("CONFIG_INVALID", "allowedOutputRoots must be explicit and non-empty");
  const url = new URL(config.endpoint);
  const local = ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (!local && url.protocol !== "https:") throw typed("CONFIG_INVALID", "Remote endpoint must use HTTPS");
  if (url.username || url.password) throw typed("CONFIG_INVALID", "Endpoint URLs must not contain credentials");
  if (config.tokenEnv && config.tokenFile) throw typed("CONFIG_INVALID", "Choose tokenEnv or tokenFile, not both");
  return { ...config, maxFileBytes: config.maxFileBytes ?? 8*MIB, maxRequestBytes: config.maxRequestBytes ?? 24*MIB, requestTimeoutMs: config.requestTimeoutMs ?? 300000 };
}

async function localFile(path: string, allowed: string[], max: number, kind: "image"|"text") {
  if (!isAbsolute(path)) throw typed("LOCAL_PATH_FORBIDDEN", "Local input path must be absolute");
  let resolved: string;
  try { resolved = await realpath(path); } catch { throw typed("INPUT_MATERIALIZATION_FAILED", "Local input is missing or unreadable"); }
  const allowedRoots = await roots(allowed);
  if (!allowedRoots.some(root => inside(resolved, root))) throw typed("LOCAL_PATH_FORBIDDEN", "Resolved local input is outside allowed input roots");
  const info = await stat(resolved);
  if (!info.isFile()) throw typed("INPUT_MATERIALIZATION_FAILED", "Local input must be a regular file");
  if (info.size > max) throw typed("INPUT_MATERIALIZATION_FAILED", "Local input exceeds per-file size limit");
  const data = await readFile(resolved);
  let mime = MIME[extname(resolved).toLowerCase()] ?? "application/octet-stream";
  if (kind === "image") {
    if (!IMAGE_MIMES.has(mime)) throw typed("UNSUPPORTED_MIME_TYPE", "Only PNG, JPEG, and WebP inputs are supported");
    try { parseImageDimensions(data, mime); } catch (error: any) { throw typed("UNSUPPORTED_MIME_TYPE", error.message); }
  } else {
    if (!["text/markdown","text/plain"].includes(mime)) throw typed("UNSUPPORTED_MIME_TYPE", "Instruction files must be Markdown or plain text");
    try { new TextDecoder("utf-8", { fatal: true }).decode(data); } catch { throw typed("UNSUPPORTED_MIME_TYPE", "Instruction file must be valid UTF-8"); }
  }
  return { data, mime, resolved };
}

export async function materializeArguments(input: Record<string, any>, limits: Limits): Promise<{wire: Record<string,any>; destination?: Destination}> {
  const wire = structuredClone(input);
  const destination = wire.client_destination as Destination | undefined;
  delete wire.client_destination;
  let total = 0;
  const references: any[] = [];
  for (const item of wire.references ?? []) {
    const wrapped = typeof item === "string" ? { source: { type: isAbsolute(item) ? "local_file" : "url", ...(isAbsolute(item) ? {path:item}:{url:item}) }, role:"content" } : structuredClone(item);
    if (wrapped?.source?.type === "local_file") {
      const file = await localFile(String(wrapped.source.path), limits.allowedInputRoots, limits.maxFileBytes, "image");
      total += file.data.length;
      if (wrapped.sha256 && wrapped.sha256 !== createHash("sha256").update(file.data).digest("hex")) throw typed("INPUT_MATERIALIZATION_FAILED", "Declared input sha256 does not match local bytes");
      wrapped.source = { type:"inline_blob", data:file.data.toString("base64"), mime_type:file.mime };
      wrapped.mime_type = file.mime;
    }
    references.push(wrapped);
  }
  if (typeof wire.mask === "string" && isAbsolute(wire.mask)) {
    const file = await localFile(wire.mask, limits.allowedInputRoots, limits.maxFileBytes, "image");
    total += file.data.length;
    references.push({ source:{type:"inline_blob",data:file.data.toString("base64"),mime_type:file.mime}, role:"mask", mime_type:file.mime });
    delete wire.mask;
  }
  wire.references = references;
  const instructions: any[] = [];
  const instructionSources = [...(wire.instruction_files ?? []), ...(wire.skills ?? []).filter((x:any)=>typeof x === "string" && isAbsolute(x)).map((path:string)=>({source:{type:"local_file",path}}))];
  if (Array.isArray(wire.skills)) wire.skills = wire.skills.filter((x:any)=>!(typeof x === "string" && isAbsolute(x)));
  for (const item of instructionSources) {
    const wrapped = typeof item === "string" ? {source:{type:"local_file",path:item}} : structuredClone(item);
    if (wrapped?.source?.type === "local_file") {
      const file = await localFile(String(wrapped.source.path), limits.allowedInputRoots, limits.maxFileBytes, "text");
      total += file.data.length;
      wrapped.source = {type:"inline_blob",data:file.data.toString("base64"),mime_type:file.mime};
    }
    instructions.push(wrapped);
  }
  wire.instruction_files = instructions;
  if (total > (limits.maxRequestBytes ?? 24*MIB)) throw typed("INPUT_MATERIALIZATION_FAILED", "Local inputs exceed per-request size limit");
  return { wire, destination };
}

async function safeDestination(destination: Destination, allowed: string[]): Promise<string> {
  const directory = destination?.path ?? destination?.directory;
  if (destination?.type && destination.type !== "local_directory") throw typed("CLIENT_WRITE_FAILED", "client_destination.type must be local_directory");
  if (typeof directory !== "string" || !isAbsolute(directory)) throw typed("CLIENT_WRITE_FAILED", "client_destination path must be absolute");
  const allowedRoots = await roots(allowed);
  let candidate = resolve(directory);
  const missing: string[] = [];
  let current: string;
  while (true) {
    try {
      const info = await lstat(candidate);
      if (!info.isDirectory() && !info.isSymbolicLink()) throw typed("CLIENT_WRITE_FAILED", "Destination path contains a non-directory");
      current = await realpath(candidate);
      break;
    } catch (error: any) {
      if (error?.code !== "ENOENT") throw error;
      const parent = dirname(candidate);
      if (parent === candidate) throw typed("CLIENT_WRITE_FAILED", "Destination is outside allowed output roots");
      missing.unshift(basename(candidate));
      candidate = parent;
    }
  }
  if (!allowedRoots.some(root => inside(current, root))) throw typed("CLIENT_WRITE_FAILED", "Destination is outside allowed output roots");

  for (const segment of missing) {
    const next = join(current, segment);
    const parent = await realpath(current);
    if (!allowedRoots.some(root => inside(parent, root))) throw typed("CLIENT_WRITE_FAILED", "Destination escapes allowed output roots");
    await mkdir(next, {mode:0o700});
    current = await realpath(next);
    if (!allowedRoots.some(root => inside(current, root))) throw typed("CLIENT_WRITE_FAILED", "Destination escapes allowed output roots");
  }
  return current;
}

export async function persistArtifacts(result: any, destination: Destination, readResource: (uri:string)=>Promise<{blob:string;mimeType?:string}>, limits: Pick<Limits,"allowedOutputRoots"|"maxFileBytes">): Promise<string[]> {
  const dir = await safeDestination(destination, limits.allowedOutputRoots);
  const artifacts = (result?.artifacts ?? []).filter((x:any)=>x.status === "completed");
  if (destination.filename && artifacts.length !== 1) throw typed("CLIENT_WRITE_FAILED", "filename is only valid for a single artifact");
  const written:string[]=[];
  for (const artifact of artifacts) {
    const resource = await readResource(String(artifact.uri));
    let data:Buffer;
    try { data = Buffer.from(resource.blob, "base64"); } catch { throw typed("ARTIFACT_READ_FAILED", "Resource blob is invalid Base64"); }
    if (data.length !== artifact.bytes || data.length > limits.maxFileBytes) throw typed("ARTIFACT_INCOMPLETE", "Artifact byte count is invalid");
    const hash = createHash("sha256").update(data).digest("hex");
    if (hash !== artifact.sha256) throw typed("ARTIFACT_INCOMPLETE", "Artifact sha256 verification failed");
    if (resource.mimeType && resource.mimeType !== artifact.mime_type) throw typed("ARTIFACT_INCOMPLETE", "Artifact MIME verification failed");
    let artifactDimensions;
    try { artifactDimensions = parseImageDimensions(data, String(artifact.mime_type)); } catch { throw typed("ARTIFACT_INCOMPLETE", "Artifact image is unreadable"); }
    if (artifactDimensions.width !== artifact.width || artifactDimensions.height !== artifact.height) throw typed("ARTIFACT_INCOMPLETE", "Artifact dimension verification failed");
    const filename = destination.filename ?? `${String(artifact.index).padStart(2,"0")}-${basename(String(artifact.name))}`;
    if (basename(filename) !== filename) throw typed("CLIENT_WRITE_FAILED", "Destination filename must not contain path separators");
    const target=join(dir,filename);
    if (!destination.overwrite) { try { await access(target, constants.F_OK); throw typed("CLIENT_WRITE_FAILED", "Destination file already exists"); } catch (e:any) { if (e?.code !== "ENOENT") throw e; } }
    const temp=join(dir,`.${filename}.${randomBytes(8).toString("hex")}.tmp`);
    const handle=await open(temp,"wx",0o600);
    try { await handle.writeFile(data); await handle.sync(); await handle.close(); await rename(temp,target); } catch(e) { await handle.close().catch(()=>{}); await unlink(temp).catch(()=>{}); throw e; }
    written.push(target);
  }
  return written;
}

export async function resolveToken(config: EdgeConfig): Promise<string|undefined> {
  if (config.tokenEnv) return process.env[config.tokenEnv];
  if (!config.tokenFile) return undefined;
  const file=resolve(config.tokenFile.replace(/^~(?=\/)/,homedir()));
  const info=await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077)!==0) throw typed("CONFIG_INVALID", "Token file must be a regular non-symlink file with mode 0600");
  return (await readFile(file,"utf8")).trim();
}
