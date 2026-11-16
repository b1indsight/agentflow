import { ApiError } from "./api";

const FILE_BYTES = 512 * 1024;
const fileId = /^[0-9a-f]{32}$/;

export type WorkerFile = {
  id: string;
  name: string | null;
  ampUrl?: string;
};

function validBasename(name: string) {
  return (
    name !== "." &&
    name !== ".." &&
    !/[/\\\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/.test(name)
  );
}

/** Accept only the canonical, fragment-free Amp object address, never amp-media-source metadata. */
export function ampAttachmentUrl(destination: string): string | null {
  const match = destination.match(
    /^https:\/\/ampcode\.com\/user-content\/attachments\/[0-9a-f]{64}-([^\s/?#\\]+)$/,
  );
  if (!match || match[0] !== destination) return null;
  try {
    return validBasename(decodeURIComponent(match[1])) ? destination : null;
  } catch {
    return null;
  }
}

/** Match before relative resolution; a basename fragment is display-only, never part of a file request. */
export function workerFileLink(
  destination: string,
  origin: string,
): WorkerFile | null {
  const match = destination.match(
    /^(https?:\/\/[^\s/?#]+)?\/api\/files\/([0-9a-f]{32})(?:#name=([^\s#]+))?$/,
  );
  if (!match || match[0] !== destination) return null;
  try {
    if (match[1]) {
      const url = new URL(match[1]);
      if (url.origin !== origin || url.username || url.password) return null;
    }
    const [encodedName, encodedAmp, ...extra] = (match[3] ?? "").split("&amp=");
    if (encodedName.length > 3072 || extra.length) return null;
    const name = encodedName ? decodeURIComponent(encodedName) : null;
    if (name && !validBasename(name)) return null;
    if (encodedAmp !== undefined) {
      if (encodedName.includes("&") || encodedAmp.includes("&")) return null;
      const ampUrl = ampAttachmentUrl(decodeURIComponent(encodedAmp));
      if (!name || !ampUrl) return null;
      return { id: match[2], name, ampUrl };
    }
    return { id: match[2], name };
  } catch {
    return null;
  }
}

/** Block resolved file requests, including server-decoded paths, without granting canonical file actions. */
export function isWorkerFileRequest(
  destination: string,
  origin: string,
): boolean {
  try {
    const url = new URL(destination);
    return (
      url.origin === origin &&
      workerFileLink(
        `${origin}${decodeURIComponent(url.pathname).replace(/\/+$/, "")}`,
        origin,
      ) !== null
    );
  } catch {
    return false;
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

export type FilePreview =
  | { kind: "text"; text: string }
  | { kind: "image"; url: string }
  | { kind: "binary" };

/** Only signed, decodable raster formats become images. Everything else is escaped UTF-8 or a download. */
export async function previewFile(
  bytes: Uint8Array<ArrayBuffer>,
): Promise<FilePreview> {
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
    // Bounded data URLs use the existing image CSP, with no object URL lifetime or HTML/SVG execution.
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

export function fileFailureMessage(error: Error) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后重试。";
    if (error.status === 403) return "没有文件读取权限，请核对登录身份。";
    if (error.status === 404)
      return "文件不存在或已被删除，请返回会话核对链接。";
    if (error.status === 409)
      return "文件已被替换，请返回会话获取重新登记的链接。";
    if (error.status === 413)
      return "文件超过 512 KiB 上限，无法预览或通过此接口下载。";
    const messages: Record<string, string> = {
      worker_unavailable: "Worker 离线或连接不可用，恢复连接后可重试。",
      query_busy: "文件读取繁忙，请稍后重试。",
      query_timeout: "Worker 读取超时，请重试。",
      file_unreadable: "Worker 无法读取文件，请核对文件权限后重试。",
      invalid_response: "Worker 返回的文件无效，请重试。",
      database_unavailable: "文件登记服务暂不可用，请重试。",
    };
    return `${messages[error.message] ?? "文件读取失败，请重试。"}（${error.message}）`;
  }
  return "文件读取失败，请检查网络或重新登录后重试。";
}
