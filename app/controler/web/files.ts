import { ApiError } from "./api";
import { jsonCacheBytes, SessionCache } from "./sessionCache";

const FILE_BYTES = 512 * 1024;
const fileId = /^[0-9a-f]{32}$/;

export type WorkerFile = { id: string; name: string | null };

type Preview =
  | { kind: "text"; text: string }
  | { kind: "image"; url: string }
  | { kind: "binary" };

export type FilePreview = { preview: Preview; size: number };
const previews = new SessionCache<string, FilePreview & { etag: string }>(
  32,
  4 * 1024 * 1024,
  jsonCacheBytes,
);

/** Only signed, decodable raster formats become images. Everything else is escaped UTF-8 or a download. */
async function previewFile(bytes: Uint8Array<ArrayBuffer>): Promise<Preview> {
  const begins = (...signature: number[]) =>
    signature.every((byte, i) => bytes[i] === byte);
  const header = new TextDecoder("ascii").decode(bytes.subarray(0, 12));
  const mime = begins(137, 80, 78, 71, 13, 10, 26, 10)
    ? "image/png"
    : begins(255, 216, 255)
      ? "image/jpeg"
      : header.startsWith("GIF87a") || header.startsWith("GIF89a")
        ? "image/gif"
        : header.startsWith("RIFF") && header.slice(8) === "WEBP"
          ? "image/webp"
          : null;
  if (mime) {
    try {
      const bitmap = await createImageBitmap(new Blob([bytes], { type: mime }));
      bitmap.close();
    } catch {
      return { kind: "binary" };
    }
    const chunks: string[] = [];
    for (let i = 0; i < bytes.length; i += 8192)
      chunks.push(String.fromCharCode(...bytes.subarray(i, i + 8192)));
    return {
      kind: "image",
      url: `data:${mime};base64,${btoa(chunks.join(""))}`,
    };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text))
      return { kind: "text", text };
  } catch {
    // Non-UTF-8 data is not lossy-decoded into misleading source text.
  }
  return { kind: "binary" };
}

/** Match before relative resolution; a basename fragment is display-only, never part of a file request. */
export function workerFileLink(
  destination: string,
  origin: string,
): WorkerFile | null {
  const match = destination.match(
    /^(https?:\/\/[^\s/?#]+)?\/api\/files\/([0-9a-f]{32})(?:#name=([^\s#]{1,3072}))?$/,
  );
  if (!match || match[0] !== destination) return null;
  try {
    if (match[1] && new URL(match[1]).origin !== origin) return null;
    const name = match[3] ? decodeURIComponent(match[3]) : null;
    if (
      name &&
      (name === "." ||
        name === ".." ||
        /[/\\\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/.test(name))
    )
      return null;
    return { id: match[2], name };
  } catch {
    return null;
  }
}

/** Only reuse a decoded preview after the authenticated endpoint confirms its contents. */
export async function readFile(
  linkId: string,
  signal: AbortSignal,
): Promise<FilePreview> {
  if (linkId.length !== 32 || !fileId.test(linkId))
    throw new TypeError("Invalid file ID");
  const cached = previews.get(linkId);
  const response = await fetch(`/api/files/${linkId}`, {
    credentials: "same-origin",
    cache: "no-store",
    headers: cached && { "If-None-Match": cached.etag },
    redirect: "error",
    signal,
  });
  const etag = response.headers.get("ETag");
  if (response.status === 304 && cached && cached.etag === etag) return cached;
  if (!response.ok) {
    let code = `请求失败 (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body.detail === "string" && body.detail) code = body.detail;
    } catch {
      // As in the JSON API client, proxy errors can have no JSON detail.
    }
    throw new ApiError(code, response.status);
  }
  const bytes = await readBytes(response, signal);
  const result = { preview: await previewFile(bytes), size: bytes.byteLength };
  signal.throwIfAborted();
  if (etag) previews.set(linkId, { ...result, etag });
  return result;
}

/** Bound actual bytes rather than trusting Content-Length. */
async function readBytes(response: Response, signal: AbortSignal) {
  if (Number(response.headers.get("Content-Length")) > FILE_BYTES) {
    await response.body?.cancel();
    throw new ApiError("file_too_large", 413);
  }
  const reader = response.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > FILE_BYTES) {
        await reader.cancel();
        throw new ApiError("file_too_large", 413);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}
