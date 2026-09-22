"use client";

import { useEffect, useState } from "react";
import { apiFetch, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";

export type UserState =
  | { status: "loading" }
  | { status: "authenticated"; user: User }
  | { status: "anonymous" };

export function useUser(): UserState {
  const [state, setState] = useState<UserState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    apiFetch<User>("/api/auth/me")
      .then((user) => {
        if (!cancelled) setState({ status: "authenticated", user });
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          setState({ status: "anonymous" });
        } else {
          setState({ status: "anonymous" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}
