import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "../App";
import { flows } from "../fixtures";
import { phases } from "../phases";
import {
  ReadingState,
  SavedDetails,
  TextWindow,
  WindowList,
} from "../windowing";
import "../theme.css";
import "../styles.css";

const board = new URL(location.href).searchParams.get("mode") === "board";
if (board) {
  flows.push(
    ...Array.from({ length: 1000 }, (_, index) => ({
      ...flows[0],
      id: 1000 + index,
      issue: 1000 + index,
      title: `长看板任务 ${index}`,
      phase: phases[index % phases.length][0],
    })),
  );
}

const root = document.getElementById("app");
if (!root) throw new Error("Missing regression harness root");
createRoot(root).render(
  <StrictMode>
    <ReadingState>
      {board ? (
        <App />
      ) : (
        <>
          <button>移出列表焦点</button>
          <div
            data-scroll-root
            role="region"
            aria-label="长列表"
            style={{ height: 500, overflow: "auto" }}
          >
            <WindowList
              count={1000}
              itemKey={(index) => index}
              label="回归列表"
            >
              {(index) => (
                <SavedDetails stateKey={`row-${index}`}>
                  <summary>记录 {index}</summary>
                  <TextWindow
                    text={"内容".repeat(3000)}
                    stateKey={`text-${index}`}
                  />
                </SavedDetails>
              )}
            </WindowList>
          </div>
        </>
      )}
    </ReadingState>
  </StrictMode>,
);
