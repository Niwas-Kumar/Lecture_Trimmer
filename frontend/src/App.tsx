import { useEffect, useState } from "react";
import { api, ApiError, Job, Me, subscribeJobs } from "./api";
import { HomePage } from "./pages/Home";
import { LecturePage } from "./pages/Lecture";
import { HelpPanel } from "./components/HelpPanel";

type Route = { page: "home" } | { page: "lecture"; id: string };

function parseRoute(): Route {
  const match = window.location.hash.match(/^#\/lecture\/([0-9a-f]{32})/);
  return match ? { page: "lecture", id: match[1] } : { page: "home" };
}

function useTheme() {
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem("theme") || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  return [theme, setTheme] as const;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [connected, setConnected] = useState(true);
  const [route, setRoute] = useState<Route>(parseRoute);
  const [now, setNow] = useState(Date.now());
  const [theme, setTheme] = useTheme();
  const [help, setHelp] = useState(false);

  useEffect(() => {
    api.me()
      .then(setMe)
      .catch((e) => (e instanceof ApiError && e.status === 401 ? setSignedOut(true) : setLoadError(e.message)));
  }, []);

  useEffect(() => {
    if (!me) return;
    api.jobs().then(setJobs).catch(() => undefined);
    return subscribeJobs(setJobs, setConnected);
  }, [me]);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.removeEventListener("hashchange", onHash);
      clearInterval(timer);
    };
  }, []);

  const open = (id: string) => {
    window.location.hash = `#/lecture/${id}`;
    window.scrollTo({ top: 0 });
  };
  const home = () => {
    window.location.hash = "#/";
  };
  const running = jobs.filter((j) => ["queued", "waiting", "running"].includes(j.status)).length;

  useEffect(() => {
    document.title = running ? `(${running}) Lecture Trimmer` : "Lecture Trimmer";
  }, [running]);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/" onClick={home}>
          <img src="/studio/logo.svg" alt="" width="34" height="34" />
          <span>
            <strong>Lecture Trimmer</strong>
            <span className="muted small">Less distraction. Complete understanding.</span>
          </span>
        </a>
        <div className="top-actions">
          {me && <span className="muted small hide-narrow">v{me.app_version} · speech: {me.speech}</span>}
          <select className="theme" value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Theme">
            <option value="system">System theme</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
          {me && <button className="ghost small" onClick={() => setHelp(true)}>Help<span className="hide-narrow"> &amp; updates</span></button>}
          {me && !me.local && (
            <form method="post" action="/auth/logout">
              <button className="ghost small" type="submit">Sign out</button>
            </form>
          )}
        </div>
      </header>

      <main>
        {signedOut && (
          <div className="page center">
            <section className="card narrow">
              <h2>Sign in to continue</h2>
              <p className="muted">Your session has ended. Sign in again to see your lectures.</p>
              <a className="button primary" href="/">Sign in</a>
            </section>
          </div>
        )}
        {loadError && (
          <div className="page center">
            <section className="card narrow error-card">
              <h2>Lecture Trimmer is not reachable</h2>
              <p className="muted">{loadError}. Check that the app is still running, then reload this page.</p>
            </section>
          </div>
        )}
        {me && route.page === "home" && <HomePage me={me} jobs={jobs} now={now} connected={connected} open={open} />}
        {me && route.page === "lecture" && (
          <LecturePage key={route.id} job={jobs.find((j) => j.id === route.id)} now={now} onBack={home} />
        )}
      </main>
      {me && help && <HelpPanel me={me} onClose={() => setHelp(false)} />}
    </div>
  );
}
