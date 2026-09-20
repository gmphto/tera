import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Provider } from "react-redux";

import App from "./App";
import { store } from "./app/store";
import "./styles.css";

// One store instance for the life of the window: `store` is created once when
// `./app/store` is first evaluated, so a retry reuses it and its `app.openedAt`.
const container = document.getElementById("root");
if (container === null) {
  throw new Error("The shell needs its #root element in index.html.");
}

createRoot(container).render(
  <StrictMode>
    <Provider store={store}>
      <App />
    </Provider>
  </StrictMode>,
);
