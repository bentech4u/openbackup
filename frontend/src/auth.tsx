import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, get, post, setCsrfToken, setUnauthorizedHandler, type Role, type User } from "./api";

interface Me {
  user: User;
  csrf_token: string;
}

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  can: (role: Role) => boolean;
}

const rank: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };
const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const qc = useQueryClient();

  const accept = useCallback((me: Me | null) => {
    setCsrfToken(me?.csrf_token ?? "");
    setUser(me?.user ?? null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      accept(await get<Me>("/api/auth/me"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) accept(null);
      else throw e;
    } finally {
      setLoading(false);
    }
  }, [accept]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      accept(null);
      qc.clear();
    });
    refresh().catch(() => setLoading(false));
  }, [refresh, accept, qc]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      refresh,
      login: async (username, password) => {
        accept(await post<Me>("/api/auth/login", { username, password }));
      },
      logout: async () => {
        try {
          await post("/api/auth/logout");
        } finally {
          accept(null);
          qc.clear();
        }
      },
      can: (role) => !!user && rank[user.role] >= rank[role],
    }),
    [user, loading, refresh, accept, qc],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
