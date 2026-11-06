import type { Element, Root, RootContent, Text } from "hast";

type Leaf = { node: Text; start: number; end: number };
const punctuation = /^[\s*_~`>#|\-\d.]*$/u;

/** Map rendered Markdown text to immutable code-point ranges, preserving hidden-source holes.
 * Only syntax belonging to a rendered node is assigned to that node. Raw HTML and
 * unrendered definitions are deliberately left unconfirmed. Each plain text leaf
 * becomes small inline spans so a paragraph taller than the viewport can be read.
 */
export function readingSource(text: string, bodyOffset: number) {
  return () => (tree: Root) => {
    const leaves: Leaf[] = [];
    const visit = (node: Root | RootContent): Leaf[] => {
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (node.type === "text") {
        if (start === undefined || end === undefined || !node.value.trim())
          return [];
        const leaf = { node, start, end };
        leaves.push(leaf);
        return [leaf];
      }
      if (!("children" in node)) return [];
      // remark gives code elements source positions, but their generated text
      // child has none. The single child owns the code (including its fences).
      const child = node.children[0];
      if (
        node.type === "element" &&
        node.tagName === "code" &&
        node.children.length === 1 &&
        child.type === "text" &&
        start !== undefined &&
        end !== undefined
      ) {
        const leaf = { node: child, start, end };
        leaves.push(leaf);
        return [leaf];
      }
      const children = node.children.flatMap(visit);
      if (!children.length || start === undefined || end === undefined)
        return children;
      const first = children[0],
        last = children[children.length - 1];
      // Links and fenced code own their destination/fence syntax, including language names.
      const ownsSyntax =
        node.type === "element" && ["a", "code", "pre"].includes(node.tagName);
      if (ownsSyntax || punctuation.test(text.slice(start, first.start)))
        first.start = start;
      if (ownsSyntax || punctuation.test(text.slice(last.end, end)))
        last.end = end;
      return children;
    };
    visit(tree);
    leaves.sort((a, b) => a.start - b.start);
    for (let i = 0; i < leaves.length; i++) {
      const leaf = leaves[i],
        previous = leaves[i - 1];
      if (previous && punctuation.test(text.slice(previous.end, leaf.start)))
        leaf.start = previous.end;
    }
    if (leaves.length) {
      if (/^\s*$/u.test(text.slice(0, leaves[0].start))) leaves[0].start = 0;
      const last = leaves[leaves.length - 1];
      if (/^\s*$/u.test(text.slice(last.end))) last.end = text.length;
    }
    const replacements = new Map(leaves.map((leaf) => [leaf.node, leaf]));
    const cp = (offset: number) =>
      bodyOffset + Array.from(text.slice(0, offset)).length;
    const rewrite = (node: Root | Element) => {
      node.children = node.children.flatMap((child): RootContent[] => {
        if (child.type === "element") rewrite(child);
        if (child.type !== "text") return [child];
        const leaf = replacements.get(child);
        if (!leaf) return [child];
        const value = child.value;
        const source = text.slice(leaf.start, leaf.end);
        const plain = source.indexOf(value);
        // Escapes/entities can change text length. Confirm that whole small node only
        // when it fits, instead of guessing source offsets inside the transformed text.
        const pieces =
          plain < 0 ? [value] : (value.match(/.{1,24}\n*|\n+/gu) ?? [value]);
        if (pieces.length > 1 && /^\n+$/u.test(pieces[0]))
          pieces.splice(0, 2, pieces[0] + pieces[1]);
        let consumed = 0;
        return pieces.map((piece, index): Element => {
          const start =
            index === 0 ? leaf.start : leaf.start + plain + consumed;
          consumed += piece.length;
          const end =
            index === pieces.length - 1
              ? leaf.end
              : leaf.start + plain + consumed;
          return {
            type: "element",
            tagName: "span",
            properties: {
              "data-read-start": cp(start),
              "data-read-end": cp(end),
            },
            children: [{ type: "text", value: piece }],
          };
        });
      });
    };
    rewrite(tree);
  };
}

/** Return actual client rectangles, intersecting every scroll/overflow clipping ancestor. */
export function visibleRegion(
  element: HTMLElement,
  root: HTMLElement,
  complete = true,
): boolean {
  if (
    element.closest("[hidden], [aria-hidden=true], [data-reading-pending=true]")
  )
    return false;
  if (getComputedStyle(element).visibility !== "visible") return false;
  let top = Math.max(0, root.getBoundingClientRect().top),
    bottom = Math.min(innerHeight, root.getBoundingClientRect().bottom),
    left = Math.max(0, root.getBoundingClientRect().left),
    right = Math.min(innerWidth, root.getBoundingClientRect().right);
  const overlay = root
    .querySelector<HTMLElement>("[data-reading-overlay]")
    ?.getBoundingClientRect();
  if (overlay && overlay.height > 0) top = Math.max(top, overlay.bottom);
  for (
    let parent = element.parentElement;
    parent && parent !== root;
    parent = parent.parentElement
  ) {
    if (
      parent instanceof HTMLDetailsElement &&
      !parent.open &&
      !parent.querySelector("summary")?.contains(element)
    )
      return false;
    const style = getComputedStyle(parent),
      rect = parent.getBoundingClientRect();
    if (style.overflowY !== "visible") {
      top = Math.max(top, rect.top);
      bottom = Math.min(bottom, rect.bottom);
    }
    if (style.overflowX !== "visible") {
      left = Math.max(left, rect.left);
      right = Math.min(right, rect.right);
    }
  }
  const rects = [...element.getClientRects()].filter(
    (rect) => rect.width > 0 && rect.height > 0,
  );
  if (!rects.length) return false;
  return complete
    ? rects.every(
        (r) =>
          r.top >= top &&
          r.bottom <= bottom &&
          r.left >= left &&
          r.right <= right,
      )
    : rects.some(
        (r) =>
          r.bottom > top && r.top < bottom && r.right > left && r.left < right,
      );
}

export function mergeReadRanges(ranges: number[][]): number[][] {
  const sorted = ranges.map(([a, b]) => [a, b]).sort((a, b) => a[0] - b[0]);
  const result: number[][] = [];
  for (const [start, end] of sorted) {
    const last = result[result.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else result.push([start, end]);
  }
  return result;
}
