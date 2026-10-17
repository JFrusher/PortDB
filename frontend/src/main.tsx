import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { Hud } from "./hud";
import { Diorama } from "./scene";
import "./styles.css";
import { connect } from "./world";

function App() {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <Diorama selected={selected} onPick={setSelected} />
      <Hud selected={selected} onPick={setSelected} />
    </>
  );
}

connect();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
