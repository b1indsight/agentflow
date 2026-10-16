import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ReadingState } from "./windowing";
import "./theme.css";
import "./styles.css";

const root = document.getElementById("app");
if (!root) throw new Error("Missing application root");
createRoot(root).render(
  <StrictMode>
    <ReadingState>
      <App />
    </ReadingState>
  </StrictMode>,
);
