"use client";

import { useEffect } from "react";
import { useIndexProgress } from "@/hooks/useIndexProgress";
import type { RunStatus } from "@/lib/types";

const PHASES: RunStatus[] = [
  "queued",
  "cloning",
  "parsing",
  "resolving",
  "embedding",
  "summarizing",
  "ready",
];

const LABELS: Record<RunStatus, string> = {
  queued: "Queued",
  cloning: "Cloning repository",
  parsing: "Parsing symbols",
  resolving: "Resolving call graph",
  embedding: "Embedding chunks",
  summarizing: "Writing overview",
  ready: "Ready",
  failed: "Failed",
};

export function IndexProgressPanel({
  runId,
  onReady,
  onFailed,
}: {
  runId: string;
  onReady?: () => void;
  onFailed?: (error: string | null) => void;
}) {
  const { frame } = useIndexProgress(runId);
  const status = frame?.status ?? "queued";
  const pct = frame?.phase_pct ?? 0;

  useEffect(() => {
    if (status === "ready") onReady?.();
    if (status === "failed") onFailed?.(frame?.error ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  if (status === "failed") {
    return (
      <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-6">
        <p className="font-medium text-red-600 dark:text-red-400">Indexing failed</p>
        <p className="mt-1 text-sm text-black/60 dark:text-white/60">
          {frame?.error ?? "Unknown error"}
        </p>
      </div>
    );
  }

  const phaseIdx = PHASES.indexOf(status);

  return (
    <div className="rounded-lg border border-black/10 p-6 dark:border-white/10">
      <div className="flex items-center justify-between">
        <p className="font-medium">{LABELS[status]}</p>
        <span className="text-sm tabular-nums text-black/50 dark:text-white/50">{pct}%</span>
      </div>
      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all duration-500"
          style={{ width: `${Math.max(4, pct)}%` }}
        />
      </div>
      <ol className="mt-5 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        {PHASES.map((phase, i) => (
          <li
            key={phase}
            className={
              i < phaseIdx
                ? "text-emerald-600 dark:text-emerald-400"
                : i === phaseIdx
                  ? "font-medium text-black dark:text-white"
                  : "text-black/35 dark:text-white/35"
            }
          >
            {LABELS[phase]}
          </li>
        ))}
      </ol>
      {frame?.message && (
        <p className="mt-4 truncate text-xs text-black/50 dark:text-white/50">{frame.message}</p>
      )}
    </div>
  );
}
