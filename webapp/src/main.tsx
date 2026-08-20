import React from "react";
import ReactDOM from "react-dom/client";
import "./index.css";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import { hydrateWindowGlass } from "./lib/windowGlass";

hydrateWindowGlass();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary label="Marionette">
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
