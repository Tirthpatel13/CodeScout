"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch, streamSse } from "@/lib/api";
import type { AgentStep, Citation, ConversationDetail, ConversationSummary, MessageRecord } from "@/lib/types";
import { AgentTrace } from "@/components/AgentTrace";
import { MessageContent } from "@/components/MessageContent";
import { FileViewer, type FileViewerTarget } from "@/components/FileViewer";

type DisplayMessage = MessageRecord & { streaming?: boolean; errorText?: string };

const CONFIDENCE_STYLE: Record<string, string> = {
  high: "text-emerald-600 dark:text-emerald-400",
  medium: "text-amber-600 dark:text-amber-400",
  low: "text-red-600 dark:text-red-400",
};

export function ChatView({
  repositoryId,
  indexRunId,
}: {
  repositoryId: string;
  indexRunId: string;
}) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fileTarget, setFileTarget] = useState<FileViewerTarget | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function refreshConversations() {
    try {
      const list = await apiFetch<ConversationSummary[]>(
        `/api/conversations?repository_id=${repositoryId}`,
      );
      setConversations(list);
    } catch {
      // non-fatal: the sidebar just stays stale
    }
  }

  useEffect(() => {
    refreshConversations();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repositoryId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  async function selectConversation(id: string) {
    if (streaming) return;
    setActiveId(id);
    setLoadError(null);
    try {
      const detail = await apiFetch<ConversationDetail>(`/api/conversations/${id}`);
      setMessages(detail.messages);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "failed to load conversation");
    }
  }

  function newChat() {
    if (streaming) return;
    setActiveId(null);
    setMessages([]);
    setLoadError(null);
  }

  async function send() {
    const text = question.trim();
    if (!text || streaming) return;
    setQuestion("");
    setStreaming(true);
    setLoadError(null);

    let conversationId = activeId;
    try {
      if (!conversationId) {
        const created = await apiFetch<{ id: string }>("/api/conversations", {
          method: "POST",
          body: JSON.stringify({
            repository_id: repositoryId,
            title: text.length > 60 ? `${text.slice(0, 60)}…` : text,
          }),
        });
        conversationId = created.id;
        setActiveId(conversationId);
        refreshConversations();
      }
    } catch (err) {
      setStreaming(false);
      setLoadError(err instanceof Error ? err.message : "failed to start conversation");
      setQuestion(text);
      return;
    }

    const userMsg: DisplayMessage = {
      id: `local-user-${Date.now()}`,
      role: "user",
      content: text,
      confidence: null,
      cost_usd: null,
      latency_ms: null,
      truncated: false,
      steps: [],
      citations: [],
    };
    const assistantMsg: DisplayMessage = {
      id: `local-assistant-${Date.now()}`,
      role: "assistant",
      content: "",
      confidence: null,
      cost_usd: null,
      latency_ms: null,
      truncated: false,
      steps: [],
      citations: [],
      streaming: true,
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);

    const controller = new AbortController();
    abortRef.current = controller;

    function updateAssistant(patch: (m: DisplayMessage) => DisplayMessage) {
      setMessages((prev) => {
        const next = [...prev];
        const idx = next.findIndex((m) => m.id === assistantMsg.id);
        if (idx !== -1) next[idx] = patch(next[idx]);
        return next;
      });
    }

    try {
      for await (const evt of streamSse(
        `/api/conversations/${conversationId}/messages`,
        { question: text },
        controller.signal,
      )) {
        switch (evt.event) {
          case "step": {
            const step = evt.data as unknown as AgentStep;
            updateAssistant((m) => ({ ...m, steps: [...m.steps, { ...step }] }));
            break;
          }
          case "step_result": {
            const result = evt.data as { idx: number; summary: string; ms: number; error: boolean };
            updateAssistant((m) => ({
              ...m,
              steps: m.steps.map((s) =>
                s.idx === result.idx
                  ? { ...s, summary: result.summary, ms: result.ms, error: result.error }
                  : s,
              ),
            }));
            break;
          }
          case "token": {
            const { text: piece } = evt.data as { text: string };
            updateAssistant((m) => ({ ...m, content: m.content + piece }));
            break;
          }
          case "done": {
            const data = evt.data as {
              confidence: DisplayMessage["confidence"];
              cost_usd: number;
              ms: number;
              truncated: boolean;
              citations: Citation[];
            };
            updateAssistant((m) => ({
              ...m,
              confidence: data.confidence,
              cost_usd: data.cost_usd,
              latency_ms: data.ms,
              truncated: data.truncated,
              citations: data.citations ?? [],
              streaming: false,
            }));
            break;
          }
          case "error": {
            const data = evt.data as { message: string };
            updateAssistant((m) => ({ ...m, streaming: false, errorText: data.message }));
            break;
          }
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        updateAssistant((m) => ({
          ...m,
          streaming: false,
          errorText: err instanceof Error ? err.message : "stream failed",
        }));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] gap-4">
      <aside className="w-56 shrink-0 overflow-y-auto border-r border-black/10 pr-3 dark:border-white/10">
        <button
          onClick={newChat}
          disabled={streaming}
          className="mb-3 w-full rounded-md border border-black/10 px-3 py-1.5 text-left text-sm font-medium transition hover:border-black/30 disabled:opacity-50 dark:border-white/15 dark:hover:border-white/30"
        >
          + New chat
        </button>
        <ul className="space-y-1">
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => selectConversation(c.id)}
                disabled={streaming}
                className={`w-full truncate rounded-md px-2 py-1.5 text-left text-sm transition disabled:opacity-50 ${
                  c.id === activeId
                    ? "bg-black/5 font-medium dark:bg-white/10"
                    : "text-black/60 hover:bg-black/5 dark:text-white/60 dark:hover:bg-white/5"
                }`}
              >
                {c.title || "Untitled"}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto pb-4">
          {messages.length === 0 && (
            <p className="mt-10 text-center text-sm text-black/40 dark:text-white/40">
              Ask anything about this codebase. Answers cite real files and lines.
            </p>
          )}
          {messages.map((m) => (
            <div key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
              <div
                className={`max-w-[85%] rounded-lg px-4 py-3 text-sm ${
                  m.role === "user"
                    ? "bg-emerald-600 text-white"
                    : "border border-black/10 dark:border-white/10"
                }`}
              >
                {m.role === "assistant" && m.steps.length > 0 && (
                  <div className="mb-2">
                    <AgentTrace steps={m.steps} live={Boolean(m.streaming) && !m.content} />
                  </div>
                )}
                {m.content && (
                  <MessageContent
                    text={m.content}
                    onCitationClick={(path, start, end) =>
                      setFileTarget({ runId: indexRunId, path, start, end })
                    }
                  />
                )}
                {m.streaming && !m.content && m.steps.length === 0 && (
                  <span className="text-black/40 dark:text-white/40">thinking…</span>
                )}
                {m.errorText && <p className="mt-2 text-red-500">{m.errorText}</p>}
                {!m.streaming && m.role === "assistant" && m.content && (
                  <div className="mt-2 flex flex-wrap gap-2 text-xs text-black/40 dark:text-white/40">
                    {m.confidence && (
                      <span className={CONFIDENCE_STYLE[m.confidence]}>
                        {m.confidence} confidence
                      </span>
                    )}
                    {typeof m.latency_ms === "number" && <span>{(m.latency_ms / 1000).toFixed(1)}s</span>}
                    {m.truncated && <span className="text-amber-500">investigation cut short</span>}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {loadError && <p className="pb-2 text-sm text-red-500">{loadError}</p>}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
          className="flex gap-2 border-t border-black/10 pt-3 dark:border-white/10"
        >
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask about this codebase…"
            disabled={streaming}
            className="flex-1 rounded-md border border-black/10 bg-transparent px-3 py-2 text-sm outline-none focus:border-emerald-500 disabled:opacity-50 dark:border-white/15"
          />
          <button
            type="submit"
            disabled={streaming || !question.trim()}
            className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-40"
          >
            {streaming ? "Asking…" : "Ask"}
          </button>
        </form>
      </div>

      <FileViewer target={fileTarget} onClose={() => setFileTarget(null)} />
    </div>
  );
}
