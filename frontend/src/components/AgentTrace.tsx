"use client";

import type { AgentStep } from "@/lib/types";

function inputPreview(input?: Record<string, unknown>): string {
  if (!input) return "";
  const parts = Object.entries(input).map(([k, v]) => `${k}=${JSON.stringify(v)}`);
  const joined = parts.join(", ");
  return joined.length > 60 ? `${joined.slice(0, 60)}…` : joined;
}

export function AgentTrace({ steps, live }: { steps: AgentStep[]; live: boolean }) {
  if (steps.length === 0) return null;
  return (
    <ol className="space-y-1.5 rounded-lg border border-black/10 bg-black/[0.02] p-3 text-xs dark:border-white/10 dark:bg-white/[0.03]">
      {steps.map((step) => (
        <li key={step.idx} className="flex items-start gap-2">
          <span
            className={`mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full ${
              step.error
                ? "bg-red-500"
                : step.summary
                  ? "bg-emerald-500"
                  : "animate-pulse bg-amber-400"
            }`}
          />
          <div className="min-w-0">
            <span className="font-mono font-medium">{step.tool}</span>
            <span className="text-black/40 dark:text-white/40">
              {" "}
              {inputPreview(step.input)}
            </span>
            {step.summary && (
              <div className="text-black/55 dark:text-white/55">
                {step.summary}
                {typeof step.ms === "number" && (
                  <span className="text-black/30 dark:text-white/30"> · {step.ms}ms</span>
                )}
              </div>
            )}
          </div>
        </li>
      ))}
      {live && (
        <li className="flex items-center gap-2 text-black/40 dark:text-white/40">
          <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-black/30 dark:bg-white/30" />
          investigating…
        </li>
      )}
    </ol>
  );
}
