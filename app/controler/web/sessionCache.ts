/** Estimate JSON keys and payloads as UTF-16 bytes, not the engine's heap footprint. Convert Maps first. */
export function jsonCacheBytes(key: string | number, value: object): number {
  return JSON.stringify([key, value]).length * 2;
}

/** Count- and byte-bounded LRU for immutable page-session data; oversized values are not retained. */
export class SessionCache<K, V> {
  private readonly entries = new Map<K, { value: V; bytes: number }>();
  private bytes = 0;

  constructor(
    private readonly limit: number,
    private readonly maxBytes: number,
    private readonly measure: (key: K, value: V) => number,
  ) {
    if (
      !Number.isSafeInteger(limit) ||
      limit < 1 ||
      !Number.isSafeInteger(maxBytes) ||
      maxBytes < 1
    )
      throw new RangeError("Cache limits must be positive safe integers");
  }

  get(key: K): V | undefined {
    const entry = this.entries.get(key);
    if (entry !== undefined) {
      this.entries.delete(key);
      this.entries.set(key, entry);
    }
    return entry?.value;
  }

  set(key: K, value: V): void {
    const bytes = this.measure(key, value);
    if (!Number.isSafeInteger(bytes) || bytes < 0)
      throw new RangeError("Cache size must be a nonnegative safe integer");
    const previous = this.entries.get(key);
    if (previous) this.bytes -= previous.bytes;
    this.entries.delete(key);
    // An oversized replacement must not leave the old snapshot available on the next visit.
    if (bytes > this.maxBytes) return;
    this.entries.set(key, { value, bytes });
    this.bytes += bytes;
    while (this.entries.size > this.limit || this.bytes > this.maxBytes) {
      const [oldest, entry] = this.entries.entries().next().value!;
      this.entries.delete(oldest);
      this.bytes -= entry.bytes;
    }
  }
}
