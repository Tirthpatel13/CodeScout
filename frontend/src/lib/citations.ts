// Mirrors backend/app/agent/schemas.py CITATION_RE.
const CITATION_RE = /\[([\w./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?\]/g;

export interface TextSegment {
  kind: "text";
  text: string;
}

export interface CitationSegment {
  kind: "citation";
  text: string;
  path: string;
  start: number;
  end: number;
}

export type MessageSegment = TextSegment | CitationSegment;

export function splitCitations(text: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(CITATION_RE)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      segments.push({ kind: "text", text: text.slice(lastIndex, index) });
    }
    const start = Number(match[2]);
    const end = match[3] ? Number(match[3]) : start;
    segments.push({ kind: "citation", text: match[0], path: match[1], start, end });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: "text", text: text.slice(lastIndex) });
  }
  return segments;
}
