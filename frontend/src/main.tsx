import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./App.css";
import { initI18n } from "./i18n";

initI18n();

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
