import { NavLink, Outlet } from "react-router-dom";

export function App(): React.JSX.Element {
  return (
    <div className="shell">
      <header className="shell__header">
        <div className="shell__brand">
          <span className="shell__mark" aria-hidden="true" />
          <span>promptproof</span>
        </div>
        <nav className="shell__nav" aria-label="Main">
          <NavLink to="/" end>
            Targets
          </NavLink>
          <NavLink to="/scans">Scans</NavLink>
        </nav>
      </header>

      <main className="shell__main">
        <Outlet />
      </main>

      <footer className="shell__footer">
        Test prompts and applications you own or are authorised to test.
      </footer>
    </div>
  );
}
