"use client";

import { splitCitations } from "@/lib/citations";

export function MessageContent({
  text,
  onCitationClick,
}: {
  text: string;
  onCitationClick: (path: string, start: number, end: number) => void;
}) {
  const segments = splitCitations(text);
  return (
    <p className="whitespace-pre-wrap leading-relaxed">
      {segments.map((segment, i) =>
        segment.kind === "text" ? (
          <span key={i}>{segment.text}</span>
        ) : (
          <button
            key={i}
            onClick={() => onCitationClick(segment.path, segment.start, segment.end)}
            className="mx-0.5 inline-flex items-center rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-xs text-emerald-700 transition hover:bg-emerald-500/20 dark:text-emerald-300"
          >
            {segment.path.split("/").pop()}:{segment.start}
            {segment.end !== segment.start ? `-${segment.end}` : ""}
          </button>
        ),
      )}
    </p>
  );
}
