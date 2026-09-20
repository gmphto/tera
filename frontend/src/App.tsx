import { useEffect } from "react";

import { LibraryBrowser } from "./features/palette/browser/LibraryBrowser";
import { PalettePanel } from "./features/palette/PalettePanel";
import { RecommendationPanel } from "./features/palette/recommendations/RecommendationPanel";
import { ServiceControls } from "./features/service/ServiceControls";
import { ServiceStatusPanel } from "./features/service/ServiceStatusPanel";
import { startBridge } from "./features/service/bridge";

/** The shell chrome. It renders whether or not the service is reachable. */
export default function App() {
  useEffect(() => startBridge(), []);

  return (
    <div className="shell">
      <header className="shell__chrome">
        <h1 className="shell__title">tera</h1>
        <p className="shell__subtitle">Local instrument palette intelligence</p>
      </header>
      <main className="shell__body">
        <ServiceStatusPanel />
        <ServiceControls />
        <PalettePanel />
        <RecommendationPanel />
        <LibraryBrowser />
      </main>
    </div>
  );
}
