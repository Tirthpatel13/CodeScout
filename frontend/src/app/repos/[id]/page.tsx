"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { apiFetch, ApiError } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { Header } from "@/components/Header";
import { IndexProgressPanel } from "@/components/IndexProgressPanel";
import { ChatView } from "@/components/ChatView";
import type { IndexRunListItem, Repository } from "@/lib/types";
import { ACTIVE_RUN_STATUSES } from "@/lib/types";

function RepoPageInner() {
  const state = useUser();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const repositoryId = params.id;

  const [repo, setRepo] = useState<Repository | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(
    searchParams.get("run"),
  );
  const [error, setError] = useState<string | null>(null);
  const [reindexing, setReindexing] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [repoData, runs] = await Promise.all([
        apiFetch<Repository>(`/api/repos/${repositoryId}`),
        apiFetch<IndexRunListItem[]>(`/api/repos/${repositoryId}/runs`),
      ]);
      setRepo(repoData);
      const active = runs.find((r) => ACTIVE_RUN_STATUSES.includes(r.status));
      setActiveRunId(active ? active.id : null);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 404
          ? "Repository not found, or you don't have access to it."
          : err instanceof Error
            ? err.message
            : "failed to load repository",
      );
    }
  }, [repositoryId]);

  useEffect(() => {
    if (state.status === "anonymous") router.replace("/");
  }, [state, router]);

  useEffect(() => {
    if (state.status === "authenticated") load();
  }, [state.status, load]);

  async function reindex() {
    if (!repo) return;
    setReindexing(true);
    setError(null);
    const [owner, name] = repo.slug.split("/");
    try {
      const { index_run_id } = await apiFetch<{ index_run_id: string }>(
        `/api/repos/${owner}/${name}/index`,
        { method: "POST" },
      );
      setActiveRunId(index_run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to start indexing");
    } finally {
      setReindexing(false);
    }
  }

  if (state.status !== "authenticated") return null;

  if (error) {
    return (
      <>
        <Header user={state.user} />
        <main className="mx-auto max-w-3xl flex-1 px-6 py-10">
          <p className="text-sm text-red-500">{error}</p>
          <a href="/repos" className="mt-4 inline-block text-sm underline">
            Back to repositories
          </a>
        </main>
      </>
    );
  }

  if (!repo) {
    return (
      <>
        <Header user={state.user} />
        <main className="mx-auto max-w-3xl flex-1 px-6 py-10 text-sm text-black/40 dark:text-white/40">
          Loading…
        </main>
      </>
    );
  }

  const stats = repo.latest_run?.stats;

  return (
    <>
      <Header user={state.user} />
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">{repo.slug}</h1>
            <p className="text-xs text-black/50 dark:text-white/50">
              {repo.default_branch}
              {repo.latest_run && ` · ${repo.latest_run.commit_sha.slice(0, 7)}`}
              {stats && (
                <>
                  {" · "}
                  {stats.files ?? 0} files · {stats.symbols ?? 0} symbols
                </>
              )}
            </p>
          </div>
          {repo.latest_run && !activeRunId && (
            <button
              onClick={reindex}
              disabled={reindexing}
              className="rounded-md border border-black/10 px-3 py-1.5 text-xs font-medium transition hover:border-black/30 disabled:opacity-40 dark:border-white/15 dark:hover:border-white/30"
            >
              {reindexing ? "Starting…" : "Re-index"}
            </button>
          )}
        </div>

        <div className="mt-6">
          {activeRunId ? (
            <div className="mx-auto max-w-xl">
              <IndexProgressPanel
                runId={activeRunId}
                onReady={() => {
                  setActiveRunId(null);
                  load();
                }}
                onFailed={() => {
                  /* keep the panel up so the error is visible */
                }}
              />
            </div>
          ) : repo.latest_run ? (
            <ChatView repositoryId={repo.id} indexRunId={repo.latest_run.id} />
          ) : (
            <div className="mx-auto max-w-xl rounded-lg border border-black/10 p-6 text-center dark:border-white/10">
              <p className="text-sm text-black/60 dark:text-white/60">
                This repository hasn&apos;t been indexed yet.
              </p>
              <button
                onClick={reindex}
                disabled={reindexing}
                className="mt-4 rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-40"
              >
                {reindexing ? "Starting…" : "Start indexing"}
              </button>
            </div>
          )}
        </div>
      </main>
    </>
  );
}

export default function RepoPage() {
  return (
    <Suspense fallback={null}>
      <RepoPageInner />
    </Suspense>
  );
}
