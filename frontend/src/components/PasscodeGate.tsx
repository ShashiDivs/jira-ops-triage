import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { LOCKED_EVENT, gateRequired, getStoredPasscode, verifyPasscode } from "../api/client";

type State = "checking" | "locked" | "open";

/** Shows its children only once the server says we may use the API. A deployment without a passcode
 * (local development) opens immediately. */
export function PasscodeGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      if (!(await gateRequired())) return alive && setState("open");
      // A passcode kept from earlier in this tab is re-checked, never trusted blindly.
      const stored = getStoredPasscode();
      if (stored && (await verifyPasscode(stored))) return alive && setState("open");
      if (alive) setState("locked");
    })();
    const lock = () => setState("locked");
    window.addEventListener(LOCKED_EVENT, lock);
    return () => {
      alive = false;
      window.removeEventListener(LOCKED_EVENT, lock);
    };
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const ok = await verifyPasscode(value);
    setBusy(false);
    if (ok) {
      setValue("");
      setState("open");
    } else {
      setError("That passcode was not accepted.");
    }
  }

  if (state === "open") return <>{children}</>;
  if (state === "checking") return <div className="gate" aria-busy="true" />;
  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1>Ops Triage</h1>
        <p>This demo is private. Enter the passcode you were given.</p>
        <input
          type="password"
          autoFocus
          placeholder="Passcode"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="Passcode"
        />
        {error && <div className="gate-error" role="alert">{error}</div>}
        <button type="submit" disabled={busy || value === ""}>
          {busy ? "Checking…" : "Enter"}
        </button>
      </form>
    </div>
  );
}
