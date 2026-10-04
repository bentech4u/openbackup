import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api";
import { AuthProvider, useAuth } from "./auth";
import Layout from "./components/Layout";
import { Loading } from "./components/ui";
import { ForcedPasswordChange, Login } from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Jobs from "./pages/Jobs";
import Tasks, { TaskDetail } from "./pages/Tasks";
import Restore, { PointPage } from "./pages/Restore";
import FileBrowser from "./pages/FileBrowser";
import VCenters from "./pages/VCenters";
import Repositories from "./pages/Repositories";
import Users from "./pages/Users";
import Audit from "./pages/Audit";
import Account from "./pages/Account";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

function Gate() {
  const { user, loading, can } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Login />;
  if (user.must_change_password) return <ForcedPasswordChange />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="tasks" element={<Tasks />} />
        <Route path="tasks/:id" element={<TaskDetail />} />
        <Route path="restore" element={<Restore />} />
        <Route path="points/:id" element={<PointPage />} />
        <Route path="points/:id/files" element={<FileBrowser />} />
        <Route path="vcenters" element={<VCenters />} />
        <Route path="repositories" element={<Repositories />} />
        {can("admin") && <Route path="users" element={<Users />} />}
        {can("admin") && <Route path="audit" element={<Audit />} />}
        <Route path="account" element={<Account />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Gate />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
