// client.ts — TypeScript API client mirroring KimixAsyncClient from client.py

import type { Message, Session, SessionStatus } from "./types";
import { parseMessage, parseSession } from "./types";

export class KimixClient {
  private host: string;
  private port: number;

  constructor(host: string = "127.0.0.1", port: number = 4096) {
    this.host = host;
    this.port = port;
  }

  get baseUrl(): string {
    return `http://${this.host}:${this.port}`;
  }

  // ── Health ────────────────────────────────────────────────

  async healthCheck(): Promise<boolean> {
    try {
      const resp = await fetch(`${this.baseUrl}/global/health`);
      const data = await resp.json();
      return !!data.healthy;
    } catch {
      return false;
    }
  }

  // ── Session CRUD ─────────────────────────────────────────

  async createSession(title?: string): Promise<Session> {
    const body = title ? JSON.stringify({ title }) : "{}";
    const resp = await fetch(`${this.baseUrl}/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!resp.ok) throw new Error(`createSession failed: ${resp.status}`);
    return parseSession(await resp.json());
  }

  async getSession(sessionId: string): Promise<Session> {
    const resp = await fetch(`${this.baseUrl}/session/${sessionId}`);
    if (!resp.ok) throw new Error(`getSession failed: ${resp.status}`);
    return parseSession(await resp.json());
  }

  async deleteSession(sessionId: string): Promise<boolean> {
    const resp = await fetch(`${this.baseUrl}/session/${sessionId}`, {
      method: "DELETE",
    });
    return resp.status === 200;
  }

  async listSessions(): Promise<Session[]> {
    const resp = await fetch(`${this.baseUrl}/session`);
    if (!resp.ok) throw new Error(`listSessions failed: ${resp.status}`);
    const data = await resp.json();
    return (data as Array<Record<string, unknown>>).map(parseSession);
  }

  async getMessages(sessionId: string, limit: number = 10): Promise<Message[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    const resp = await fetch(
      `${this.baseUrl}/session/${sessionId}/message?${params}`
    );
    if (!resp.ok) throw new Error(`getMessages failed: ${resp.status}`);
    const data = await resp.json();
    return (data as Array<Record<string, unknown>>).map(parseMessage);
  }

  async getSessionStatus(sessionId: string): Promise<SessionStatus> {
    const resp = await fetch(`${this.baseUrl}/session/${sessionId}/status`);
    if (!resp.ok) throw new Error(`getSessionStatus failed: ${resp.status}`);
    return await resp.json();
  }

  // ── Messaging ────────────────────────────────────────────

  async sendPromptAsync(
    sessionId: string,
    text: string,
    agent?: string
  ): Promise<boolean> {
    const body: Record<string, unknown> = {
      parts: [{ type: "text", text }],
    };
    if (agent) body.agent = agent;

    const resp = await fetch(
      `${this.baseUrl}/session/${sessionId}/prompt_async`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
    return resp.status === 204;
  }

  async abortSession(sessionId: string): Promise<boolean> {
    const resp = await fetch(`${this.baseUrl}/session/${sessionId}/abort`, {
      method: "POST",
    });
    return resp.status === 200;
  }

  async sendPlan(sessionId: string, text: string): Promise<boolean> {
    const body = {
      parts: [{ type: "text", text }],
    };
    const resp = await fetch(
      `${this.baseUrl}/session/${sessionId}/plan`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
    return resp.status === 204;
  }

  async clearSession(sessionId: string): Promise<boolean> {
    const resp = await fetch(`${this.baseUrl}/session/${sessionId}/clear`);
    return resp.status === 200;
  }

  async compactSession(
    sessionId: string,
    keep?: number
  ): Promise<boolean> {
    const params = new URLSearchParams();
    if (keep !== undefined) params.set("keep", String(keep));
    const qs = params.toString();
    const url = `${this.baseUrl}/session/${sessionId}/compact${qs ? "?" + qs : ""}`;
    const resp = await fetch(url);
    return resp.status === 200;
  }

  async exportSession(
    sessionId: string,
    outputPath?: string
  ): Promise<Record<string, unknown>> {
    const params = new URLSearchParams();
    if (outputPath) params.set("output_path", outputPath);
    const qs = params.toString();
    const url = `${this.baseUrl}/session/${sessionId}/export${qs ? "?" + qs : ""}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`exportSession failed: ${resp.status}`);
    return await resp.json();
  }

  // ── SSE Streaming ────────────────────────────────────────

  /** Open an EventSource connection to the /event SSE endpoint.
   *  Returns the EventSource and a cleanup function. */
  streamEvents(
    onEvent: (data: string, eventType: string) => void,
    onError?: (err: Event) => void
  ): { close: () => void } {
    const url = `${this.baseUrl}/event`;
    const es = new EventSource(url);

    es.onmessage = (event: MessageEvent) => {
      onEvent(event.data as string, "");
    };

    es.onerror = (event: Event) => {
      if (onError) onError(event);
    };

    // Listen for specific event types too
    es.addEventListener("__reconnected__", (event: Event) => {
      const me = event as MessageEvent;
      onEvent(me.data || "", "__reconnected__");
    });

    return {
      close: () => es.close(),
    };
  }
}
