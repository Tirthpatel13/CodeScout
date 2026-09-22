"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { FileSlice } from "@/lib/types";

export interface FileViewerTarget {
  runId: string;
  path: string;
  start: number;
  end: number;
}

export function FileViewer({
  target,
  onClose,
}: {
  target: FileViewerTarget | null;
  onClose: () => void;
}) {
  const [slice, setSlice] = useState<FileSlice | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    setSlice(null);
    setError(null);
    const context = 8;
    const start = Math.max(1, target.start - context);
    const end = target.end + context;
    apiFetch<FileSlice>(
      `/api/index-runs/${target.runId}/files/${target.path}?start=${start}&end=${end}`,
    )
      .then(setSlice)
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load file"));
  }, [target]);

  if (!target) return null;

  const lines = slice?.content.split("\n") ?? [];

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-2xl flex-col bg-white shadow-xl dark:bg-neutral-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-black/10 px-4 py-3 dark:border-white/10">
          <div className="min-w-0">
            <p className="truncate font-mono text-sm font-medium">{target.path}</p>
            <p className="text-xs text-black/50 dark:text-white/50">
              lines {target.start}-{target.end}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-black/50 hover:bg-black/5 dark:text-white/50 dark:hover:bg-white/10"
          >
            Close
          </button>
        </div>
        <div className="flex-1 overflow-auto">
          {error && <p className="p-4 text-sm text-red-500">{error}</p>}
          {!error && !slice && (
            <p className="p-4 text-sm text-black/50 dark:text-white/50">Loading…</p>
          )}
          {slice && (
            <pre className="min-w-full text-xs leading-5">
              {lines.map((line, i) => {
                const lineNo = slice.start + i;
                const cited = lineNo >= target.start && lineNo <= target.end;
                return (
                  <div
                    key={lineNo}
                    className={`flex px-4 ${cited ? "bg-emerald-500/10" : ""}`}
                  >
                    <span className="w-10 shrink-0 select-none pr-3 text-right text-black/30 dark:text-white/30">
                      {lineNo}
                    </span>
                    <code className="whitespace-pre-wrap break-all">{line || " "}</code>
                  </div>
                );
              })}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
