import type { ReactNode } from "react";
import { highlightCode, tagHighlighter, tags } from "@lezer/highlight";
import { parser as css } from "@lezer/css";
import { parser as html } from "@lezer/html";
import { parser as js } from "@lezer/javascript";
import { parser as json } from "@lezer/json";
import { parser as markdown } from "@lezer/markdown";
import { parser as python } from "@lezer/python";
import { parser as rust } from "@lezer/rust";
import { parser as xml } from "@lezer/xml";
import { parser as yaml } from "@lezer/yaml";

const parsers = new Map(
  Object.entries({
    css,
    html,
    htm: html,
    js,
    mjs: js,
    cjs: js,
    jsx: js.configure({ dialect: "jsx" }),
    ts: js.configure({ dialect: "ts" }),
    tsx: js.configure({ dialect: "ts jsx" }),
    json,
    md: markdown,
    markdown,
    py: python,
    rs: rust,
    xml,
    svg: xml,
    yaml,
    yml: yaml,
  }),
);

const colors = tagHighlighter([
  { tag: [tags.keyword, tags.tagName, tags.heading], class: "syntax-keyword" },
  { tag: [tags.string, tags.link], class: "syntax-string" },
  {
    tag: [
      tags.number,
      tags.bool,
      tags.null,
      tags.propertyName,
      tags.attributeName,
    ],
    class: "syntax-value",
  },
  { tag: tags.comment, class: "syntax-comment" },
]);

export function parseSource(filename: string | null, text: string) {
  const extension = filename?.match(/\.([^.]+)$/)?.[1].toLowerCase();
  return extension ? parsers.get(extension)?.parse(text) : undefined;
}

/** Parse context spans the file, but create React nodes only for the mounted range. */
export function sourceLines(
  text: string,
  tree: ReturnType<typeof parseSource>,
  from: number,
  to: number,
): ReactNode[] {
  if (!tree) return text.slice(from, to).match(/[^\n]*\n|[^\n]+$/g) ?? [""];
  const lines: ReactNode[][] = [];
  let line: ReactNode[] = [];
  highlightCode(
    text,
    tree,
    colors,
    (part, className) => {
      line.push(
        className ? (
          <span className={className} key={line.length}>
            {part}
          </span>
        ) : (
          part
        ),
      );
    },
    () => {
      line.push("\n");
      lines.push(line);
      line = [];
    },
    from,
    to,
  );
  if (line.length || !lines.length) lines.push(line);
  return lines;
}
