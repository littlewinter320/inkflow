import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppBoundary } from "./AppBoundary";
import "./styles.css";
import "./studio-design.css";
import "./studio-palettes.css";
import "./studio-motion.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppBoundary><App /></AppBoundary>
  </StrictMode>,
);
