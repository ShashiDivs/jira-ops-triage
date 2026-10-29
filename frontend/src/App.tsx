import { useState } from "react";
import { resetDemoState } from "./api/client";
import { PasscodeGate } from "./components/PasscodeGate";
import { PendingApprovals } from "./pages/PendingApprovals";
import { SubmitTicket } from "./pages/SubmitTicket";
import { TicketLog } from "./pages/TicketLog";

type Tab = "submit" | "pending" | "log";

function Dashboard() {
  const [tab, setTab] = useState<Tab>("submit");
  const [resetting, setResetting] = useState(false);

  async function handleReset() {
    setResetting(true);
    try {
      await resetDemoState();
      window.location.reload();
    } finally {
      setResetting(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Ops Triage</h1>
        <nav>
          <button className={tab === "submit" ? "active" : ""} onClick={() => setTab("submit")}>
            Submit
          </button>
          <button className={tab === "pending" ? "active" : ""} onClick={() => setTab("pending")}>
            Pending approvals
          </button>
          <button className={tab === "log" ? "active" : ""} onClick={() => setTab("log")}>
            Ticket log
          </button>
        </nav>
        <button className="reset" disabled={resetting} onClick={handleReset}>
          Reset demo state
        </button>
      </header>
      <main>
        {tab === "submit" && <SubmitTicket />}
        {tab === "pending" && <PendingApprovals />}
        {tab === "log" && <TicketLog />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <PasscodeGate>
      <Dashboard />
    </PasscodeGate>
  );
}
