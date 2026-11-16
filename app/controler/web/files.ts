import { ApiError } from "./api";

const FILE_BYTES = 512 * 1024;
const fileId = /^[0-9a-f]{32}$/;

export type WorkerFile = { id: string; name: string | null };

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

/** Read the existing authenticated attachment endpoint, bounding actual bytes rather than trusting headers. */
export async function readFile(
  linkId: string,
  signal: AbortSignal,
): Promise<Uint8Array<ArrayBuffer>> {
  if (linkId.length !== 32 || !fileId.test(linkId))
    throw new TypeError("Invalid file ID");
  const response = await fetch(`/api/files/${linkId}`, {
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    signal,
  });
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
