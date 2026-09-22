"use client";

import { useEffect, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { ProgressFrame } from "@/lib/types";

/**
 * Subscribes to /api/ws/index-runs/{runId} while runId is non-null and the
 * run hasn't reached a terminal state. The server sends a snapshot on
 * connect, then live phase updates, then closes after ready/failed.
 */
export function useIndexProgress(runId: string | null): {
  frame: ProgressFrame | null;
  connected: boolean;
} {
  const [frame, setFrame] = useState<ProgressFrame | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!runId) {
      setFrame(null);
      setConnected(false);
      return;
    }

    let closedByUs = false;
    const socket = new WebSocket(wsUrl(`/api/ws/index-runs/${runId}`));

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onerror = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as ProgressFrame;
        if (data.type === "ping") return;
        setFrame(data);
        if (data.status === "ready" || data.status === "failed") {
          closedByUs = true;
          socket.close();
        }
      } catch {
        // ignore malformed frames
      }
    };

    return () => {
      if (!closedByUs) socket.close();
    };
  }, [runId]);

  return { frame, connected };
}
