import { NavLink, Outlet } from "react-router-dom";
import {
  Boxes,
  Briefcase,
  Database,
  History,
  LayoutDashboard,
  LogOut,
  RotateCcw,
  ScrollText,
  Server,
  UserCog,
  Users,
} from "lucide-react";
import { useAuth } from "../auth";

export default function Layout() {
  const { user, logout, can } = useAuth();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/logo.png" alt="OpenBackup" className="brand-logo" />
        </div>
        <nav className="nav">
          <NavLink to="/" end>
            <LayoutDashboard size={17} /> Dashboard
          </NavLink>
          <NavLink to="/jobs">
            <Briefcase size={17} /> Backup jobs
          </NavLink>
          <NavLink to="/tasks">
            <History size={17} /> History
          </NavLink>
          <NavLink to="/restore">
            <RotateCcw size={17} /> Restore
          </NavLink>
          <div className="nav-section">Infrastructure</div>
          <NavLink to="/vcenters">
            <Server size={17} /> vCenters
          </NavLink>
          <NavLink to="/clusters">
            <Boxes size={17} /> OpenShift
          </NavLink>
          <NavLink to="/repositories">
            <Database size={17} /> Repositories
          </NavLink>
          {can("admin") && (
            <>
              <div className="nav-section">Administration</div>
              <NavLink to="/users">
                <Users size={17} /> Users
              </NavLink>
              <NavLink to="/audit">
                <ScrollText size={17} /> Audit log
              </NavLink>
            </>
          )}
        </nav>
        <div className="side-foot stack" style={{ gap: 8 }}>
          <div>
            <div className="who">{user?.full_name || user?.username}</div>
            <div className="small" style={{ textTransform: "capitalize" }}>{user?.role}</div>
          </div>
          <div className="row gap-m">
            <NavLink to="/account" style={{ color: "inherit" }} className="row gap-s">
              <UserCog size={15} /> Account
            </NavLink>
            <button onClick={() => logout()}>
              <LogOut size={15} /> Sign out
            </button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
