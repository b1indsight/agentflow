import type { Activity, Block } from "./types";
import {
  labelPreview,
  SavedDetails,
  TextWindow,
  WindowList,
} from "./windowing";

function BlockView({ block, stateKey }: { block: Block; stateKey: string }) {
  switch (block.kind) {
    case "paragraph":
      return (
        <p>
          <TextWindow text={block.text} stateKey={`${stateKey}-text`} />
        </p>
      );
    case "heading":
      return (
        <h3>
          <TextWindow text={block.text} stateKey={`${stateKey}-text`} />
        </h3>
      );
    case "verdict":
      return (
        <span className="review-verdict">
          <TextWindow text={block.text} stateKey={`${stateKey}-text`} />
        </span>
      );
    case "tool":
      return (
        <SavedDetails className="tool" stateKey={`${stateKey}-open`}>
          <summary>
            <span aria-hidden="true">⌘</span>
            <span className="tool-name">{labelPreview(block.name)}</span>
            <span>{labelPreview(block.status, 40)}</span>
          </summary>
          <TextWindow
            text={block.output}
            stateKey={`${stateKey}-output`}
            code
            label={`${labelPreview(block.name)}输出`}
          />
        </SavedDetails>
      );
    case "thread":
      return (
        <SavedDetails
          className="review-thread"
          stateKey={`${stateKey}-open`}
          open
        >
          <summary>
            {labelPreview(block.path)} · {labelPreview(block.lines)} ·{" "}
            {block.replies.length} 条评论
            {block.resolved ? " · 已解决" : ""}
          </summary>
          <TextWindow
            text={block.code}
            stateKey={`${stateKey}-code`}
            code
            label="代码上下文"
          />
          <WindowList
            count={block.replies.length}
            label="Review 回复"
            itemKey={(index) => `${stateKey}-reply-${index}`}
            estimate={130}
          >
            {(index) => {
              const reply = block.replies[index];
              return (
                <div
                  className="thread-comment"
                  key={`${reply.author}-${reply.time}`}
                >
                  <strong>{labelPreview(reply.author)}</strong>
                  <time>{labelPreview(reply.time)}</time>
                  <p>
                    <TextWindow
                      text={reply.text}
                      stateKey={`${stateKey}-reply-${index}-text`}
                    />
                  </p>
                </div>
              );
            }}
          </WindowList>
        </SavedDetails>
      );
  }
}

/** A stable activity node keeps native details expansion through stream updates. */
export function ActivityView({
  entry,
  stateKey = entry.id,
}: {
  entry: Activity;
  stateKey?: string;
}) {
  const body = (
    <WindowList
      count={entry.blocks.length}
      label="会话正文块"
      itemKey={(index) => `${stateKey}-block-${index}`}
      estimate={150}
    >
      {(index) => (
        <BlockView
          block={entry.blocks[index]}
          stateKey={`${stateKey}-block-${index}`}
        />
      )}
    </WindowList>
  );
  if (entry.kind === "event")
    return (
      <div className="activity-event" data-entry={entry.id}>
        <span className="event-mark" aria-hidden="true">
          ◇
        </span>
        <div className="event-body">
          {labelPreview(entry.source)} · {labelPreview(entry.author)}
          {body}
        </div>
        <time>{labelPreview(entry.time)}</time>
      </div>
    );
  const assistant = entry.kind === "agent";
  return (
    <article className="message" data-entry={entry.id}>
      <div
        className={`message-avatar${assistant ? " assistant" : ""}`}
        aria-hidden="true"
      >
        {assistant ? "⌁" : entry.author.slice(0, 1).toUpperCase()}
      </div>
      <div className="message-main">
        <div className="message-label">
          <span>{labelPreview(entry.author)}</span>
          <span className="source-tag">{labelPreview(entry.source)}</span>
          <time>{labelPreview(entry.time)}</time>
        </div>
        {entry.kind === "github" ? (
          <div className="github-bubble">{body}</div>
        ) : (
          body
        )}
      </div>
    </article>
  );
}
