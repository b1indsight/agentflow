import { memo, type ReactNode } from "react";
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

const ts = js.configure({ dialect: "ts" });
const languageParsers = new Map(
  Object.entries({
    css,
    html,
    htm: html,
    js,
    javascript: js,
    mjs: js,
    cjs: js,
    jsx: js.configure({ dialect: "jsx" }),
    ts,
    typescript: ts,
    tsx: js.configure({ dialect: "ts jsx" }),
    json,
    md: markdown,
    markdown,
    py: python,
    python,
    rs: rust,
    rust,
    xml,
    svg: xml,
    yaml,
    yml: yaml,
  }),
);

export const syntaxColors = tagHighlighter([
  { tag: tags.keyword, class: "syntax-keyword" },
  { tag: [tags.string, tags.link], class: "syntax-string" },
  { tag: tags.regexp, class: "syntax-regexp" },
  {
    tag: [
      tags.function(tags.variableName),
      tags.function(tags.propertyName),
      tags.typeName,
      tags.className,
    ],
    class: "syntax-entity",
  },
  {
    tag: [
      tags.number,
      tags.bool,
      tags.null,
      tags.propertyName,
      tags.attributeName,
      tags.tagName,
      tags.heading,
    ],
    class: "syntax-value",
  },
  { tag: tags.comment, class: "syntax-comment" },
]);

export function parserForFile(filename: string | null) {
  const extension = filename?.match(/\.([^.]+)$/)?.[1].toLowerCase();
  return extension ? languageParsers.get(extension) : undefined;
}

/** Parse context spans the file, but create React nodes only for the mounted range. */
export function sourceLines(
  text: string,
  tree: ReturnType<typeof js.parse> | undefined,
  from: number,
  to: number,
): ReactNode[] {
  if (!tree) return text.slice(from, to).match(/[^\n]*\n|[^\n]+$/g) ?? [""];
  const lines: ReactNode[][] = [];
  let line: ReactNode[] = [];
  highlightCode(
    text,
    tree,
    syntaxColors,
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

export default memo(function FencedCode({
  language,
  text,
}: {
  language: string;
  text: string;
}) {
  const tree = languageParsers.get(language)?.parse(text);
  return (
    <code className={`language-${language}`}>
      {tree ? sourceLines(text, tree, 0, text.length) : text}
    </code>
  );
});
