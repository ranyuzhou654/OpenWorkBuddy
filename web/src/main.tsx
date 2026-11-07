import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "motion/react";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource-variable/source-serif-4";
import { App } from "./App";
import { theme } from "./api";
import { UIProvider } from "./ui";
import "./style.css";

theme.apply();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <UIProvider>
        <App />
      </UIProvider>
    </MotionConfig>
  </StrictMode>,
);
