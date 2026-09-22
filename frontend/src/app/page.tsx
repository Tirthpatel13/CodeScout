"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, ApiError } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { Header } from "@/components/Header";

export default function HomePage() {
  const state = useUser();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (state.status === "authenticated") router.replace("/repos");
  }, [state, router]);

  async function signIn() {
    setError(null);
    setStarting(true);
    try {
      const { url, state: oauthState } = await apiFetch<{ url: string; state: string }>(
        "/api/auth/github/start",
      );
      sessionStorage.setItem("codescout_oauth_state", oauthState);
      window.location.href = url;
    } catch (err) {
      setStarting(false);
      setError(
        err instanceof ApiError && err.status === 501
          ? "GitHub OAuth isn't configured on this server yet — set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET."
          : err instanceof Error
            ? err.message
            : "failed to start sign in",
      );
    }
  }

  return (
    <>
      <Header />
      <main className="mx-auto flex max-w-3xl flex-1 flex-col items-center justify-center px-6 py-24 text-center">
        <h1 className="text-4xl font-semibold tracking-tight">
          Ask questions about an unfamiliar codebase
        </h1>
        <p className="mt-4 max-w-xl text-black/60 dark:text-white/60">
          Point CodeScout at a GitHub repository. It builds a symbol graph, investigates with an
          agent that greps, reads files, and walks the call graph, then answers with citations you
          can click straight to the line.
        </p>
        <button
          onClick={signIn}
          disabled={starting || state.status === "loading"}
          className="mt-8 flex items-center gap-2 rounded-md bg-black px-5 py-2.5 text-sm font-medium text-white transition hover:bg-black/80 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-white/80"
        >
          <svg viewBox="0 0 16 16" className="h-4 w-4 fill-current" aria-hidden="true">
            <path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38l-.01-1.49c-2.01.44-2.43-.97-2.43-.97-.33-.83-.8-1.05-.8-1.05-.66-.45.05-.44.05-.44.72.05 1.1.74 1.1.74.64 1.1 1.68.78 2.09.6.07-.46.25-.78.46-.96-1.6-.18-3.29-.8-3.29-3.57 0-.79.28-1.43.74-1.94-.07-.18-.32-.92.07-1.92 0 0 .6-.19 1.98.74a6.9 6.9 0 0 1 3.6 0c1.38-.93 1.98-.74 1.98-.74.39 1 .14 1.74.07 1.92.46.51.74 1.15.74 1.94 0 2.78-1.7 3.39-3.31 3.57.26.22.49.66.49 1.33l-.01 1.98c0 .21.14.45.55.38A8 8 0 0 0 8 0Z" />
          </svg>
          {starting ? "Redirecting…" : "Sign in with GitHub"}
        </button>
        {error && <p className="mt-4 max-w-md text-sm text-red-500">{error}</p>}
      </main>
    </>
  );
}
