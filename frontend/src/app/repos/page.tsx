"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, ApiError } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { Header } from "@/components/Header";
import type { GitHubRepoSummary, IndexRunDetail } from "@/lib/types";

export default function ReposPage() {
  const state = useUser();
  const router = useRouter();
  const [repos, setRepos] = useState<GitHubRepoSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [starting, setStarting] = useState<string | null>(null);

  useEffect(() => {
    if (state.status === "anonymous") router.replace("/");
  }, [state, router]);

  useEffect(() => {
    if (state.status !== "authenticated") return;
    setLoading(true);
    apiFetch<GitHubRepoSummary[]>("/api/github/repos")
      .then(setRepos)
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load repos"))
      .finally(() => setLoading(false));
  }, [state.status]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter((r) => `${r.owner}/${r.name}`.toLowerCase().includes(q));
  }, [repos, filter]);

  async function startIndex(repo: GitHubRepoSummary) {
    const key = `${repo.owner}/${repo.name}`;
    setStarting(key);
    setError(null);
    try {
      const { index_run_id } = await apiFetch<{ index_run_id: string }>(
        `/api/repos/${repo.owner}/${repo.name}/index`,
        { method: "POST" },
      );
      const run = await apiFetch<IndexRunDetail>(`/api/index-runs/${index_run_id}`);
      router.push(`/repos/${run.repository_id}?run=${run.id}`);
    } catch (err) {
      setStarting(null);
      setError(
        err instanceof ApiError && err.status === 429
          ? "You've hit the indexing rate limit — try again in a bit."
          : err instanceof Error
            ? err.message
            : "failed to start indexing",
      );
    }
  }

  if (state.status !== "authenticated") return null;

  return (
    <>
      <Header user={state.user} />
      <main className="mx-auto w-full max-w-3xl flex-1 px-6 py-10">
        <h1 className="text-xl font-semibold">Pick a repository</h1>
        <p className="mt-1 text-sm text-black/60 dark:text-white/60">
          Indexing clones the repo, parses it into a symbol graph, and embeds it for search. This
          runs once per commit.
        </p>

        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter repositories…"
          className="mt-6 w-full rounded-md border border-black/10 bg-transparent px-3 py-2 text-sm outline-none focus:border-emerald-500 dark:border-white/15"
        />

        {error && <p className="mt-4 text-sm text-red-500">{error}</p>}

        {loading && <p className="mt-8 text-sm text-black/40 dark:text-white/40">Loading repositories…</p>}

        <ul className="mt-4 divide-y divide-black/10 dark:divide-white/10">
          {filtered.map((repo) => {
            const key = `${repo.owner}/${repo.name}`;
            return (
              <li key={repo.github_id} className="flex items-center justify-between gap-4 py-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{key}</p>
                  {repo.description && (
                    <p className="truncate text-xs text-black/50 dark:text-white/50">
                      {repo.description}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => startIndex(repo)}
                  disabled={starting !== null}
                  className="shrink-0 rounded-md border border-black/10 px-3 py-1.5 text-xs font-medium transition hover:border-emerald-500 hover:text-emerald-600 disabled:opacity-40 dark:border-white/15 dark:hover:text-emerald-400"
                >
                  {starting === key ? "Starting…" : "Index"}
                </button>
              </li>
            );
          })}
        </ul>

        {!loading && filtered.length === 0 && !error && (
          <p className="mt-8 text-sm text-black/40 dark:text-white/40">No repositories found.</p>
        )}
      </main>
    </>
  );
}
