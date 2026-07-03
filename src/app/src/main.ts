// main.ts — Main application logic mirroring _sse_cli_main from sse_cli.py

import { KimixClient } from "./client";
import {
  renderMessagePart,
  createThinkingStreamElement,
  updateThinkingStreamElement,
  createTextStreamContainer,
  finalizeTextStreamElement,
} from "./renderer";
import { MessagePartType } from "./types";
import type { Message, Session, SessionStatus, StreamingPartState } from "./types";

// ── Application State ──────────────────────────────────────────────

let client: KimixClient | null = null;
let session: Session | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let emptyPolls = 0;
let connected = false;
let debugMode = false;
let pendingPlanMode = false;

// Track rendered part IDs for deduplication, mapped to their DOM elements for in-place updates
let renderedPartMap: Map<string, HTMLElement> = new Map();
// Track rendered message IDs so cumulative backends (opencode format) don't repeat
// already-shown messages. Dummy format has empty message IDs and is draining, so those
// messages are always rendered.
let renderedMessageIds: Set<string> = new Set();

// Track in-progress streaming parts for realtime SSE updates
let streamingParts: Map<string, StreamingPartState> = new Map();

// ── Context Usage Tracking (mirrors _print_transition_usage from base.py) ─

let previousPartType: string | null = null;
let lastContextUsage: number = 0;
let lastContextTokens: number = 0;

/** Update the cached context usage from a SessionStatus object. */
function updateContextUsage(status: SessionStatus): void {
  if (status.context_usage !== undefined) {
    lastContextUsage = status.context_usage;
  }
  if (status.token_count !== undefined) {
    lastContextTokens = status.token_count;
  }
}

/**
 * Format the context usage string like base.py `percentage_and_token`.
 * Returns e.g. "45.0% (1234 tokens)"
 */
function formatContextUsage(): string {
  return `${(lastContextUsage * 100).toFixed(1)}% (${lastContextTokens} tokens)`;
}

/**
 * Create a context-usage bar element mirroring `_print_transition_usage`.
 * Format:  ==================== Context usage: 45.0% (1234 tokens) ====================
 */
function renderContextUsageBar(): HTMLElement | null {
  if (lastContextTokens === 0 && lastContextUsage === 0) return null;
  const splitStr = "=".repeat(20);
  const usage = formatContextUsage();
  const text = `Context usage: ${usage}`;
  const targetWidth = 80;
  const left = `${splitStr} ${text} `;
  const rightLen = Math.max(targetWidth - left.length, 1);
  const rightSplit = "=".repeat(rightLen);
  const el = document.createElement("div");
  el.className = "context-usage-bar";
  el.textContent = `${left}${rightSplit}`;
  return el;
}

/**
 * Check if the part type has changed from the previous one and, if so,
 * insert a context usage bar. Mirrors `_print_transition_usage`.
 */
function maybePrintContextTransition(partType: string): void {
  if (previousPartType !== null && previousPartType !== partType) {
    const bar = renderContextUsageBar();
    if (bar) {
      const wrapper = document.createElement("div");
      wrapper.className = "log-line";
      wrapper.appendChild(bar);
      outputEl.appendChild(wrapper);
      scrollToBottom();
    }
  }
  previousPartType = partType;
}

// ── Streaming Helpers ───────────────────────────────────────────

/** Finalize any active TEXT streaming part (render accumulated markdown). */
function finalizePreviousTextStream(): void {
  for (const [_pid, state] of streamingParts) {
    if (state.type === MessagePartType.TEXT && !state.isComplete) {
      state.isComplete = true;
      const textEl = state.element?.querySelector(".part-text") as HTMLElement | null;
      if (textEl) {
        finalizeTextStreamElement(textEl, state.accumulatedText);
      }
      break;
    }
  }
}

/** Mark any active THINKING stream as complete (remove streaming indicator). */
function finalizeActiveThinkingStream(): void {
  for (const [_pid, state] of streamingParts) {
    if (state.type === MessagePartType.THINKING && !state.isComplete) {
      state.isComplete = true;
      const el = state.element?.querySelector(".part-thinking") as HTMLElement | null;
      if (el) {
        delete el.dataset.streaming;
      }
      break;
    }
  }
}

/** Finalize ALL active streaming parts (both TEXT and THINKING). */
function finalizeAllStreamingParts(): void {
  for (const [_pid, state] of streamingParts) {
    if (state.isComplete) continue;
    state.isComplete = true;
    if (state.type === MessagePartType.TEXT) {
      const textEl = state.element?.querySelector(".part-text") as HTMLElement | null;
      if (textEl) {
        finalizeTextStreamElement(textEl, state.accumulatedText);
      }
    } else if (state.type === MessagePartType.THINKING) {
      const el = state.element?.querySelector(".part-thinking") as HTMLElement | null;
      if (el) {
        delete el.dataset.streaming;
      }
    }
  }
}

// SSE streaming state
let sseConnection: { close: () => void } | null = null;

// ── DOM Elements ──────────────────────────────────────────────────

const hostInput = document.getElementById("host") as HTMLInputElement;
const portInput = document.getElementById("port") as HTMLInputElement;
const connectBtn = document.getElementById("connect-btn") as HTMLButtonElement;
const disconnectBtn = document.getElementById("disconnect-btn") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLElement;
const sessionIdEl = document.getElementById("session-id") as HTMLElement;
const outputEl = document.getElementById("output") as HTMLElement;
const promptInput = document.getElementById("prompt") as HTMLTextAreaElement;
const sendBtn = document.getElementById("send-btn") as HTMLButtonElement;
const debugCheck = document.getElementById("debug-check") as HTMLInputElement;

// Command buttons
const btnNew = document.getElementById("btn-new") as HTMLButtonElement;
const btnAbort = document.getElementById("btn-abort") as HTMLButtonElement;
const btnStatus = document.getElementById("btn-status") as HTMLButtonElement;
const btnSessions = document.getElementById("btn-sessions") as HTMLButtonElement;
const btnMessages = document.getElementById("btn-messages") as HTMLButtonElement;
const btnClear = document.getElementById("btn-clear") as HTMLButtonElement;
const btnCompact = document.getElementById("btn-compact") as HTMLButtonElement;
const btnExport = document.getElementById("btn-export") as HTMLButtonElement;
const btnContext = document.getElementById("btn-context") as HTMLButtonElement;
const btnPlan = document.getElementById("btn-plan") as HTMLButtonElement;

// ── Output Helpers ────────────────────────────────────────────────

function log(text: string, cls: string = ""): void {
  const line = document.createElement("div");
  line.className = `log-line ${cls}`;
  line.textContent = text;
  outputEl.appendChild(line);
  scrollToBottom();
}

function logHtml(html: string, cls: string = ""): void {
  const line = document.createElement("div");
  line.className = `log-line ${cls}`;
  line.innerHTML = html;
  outputEl.appendChild(line);
  scrollToBottom();
}

function appendPart(partEl: HTMLElement, partId?: string): boolean {
  if (partId) {
    // Check if this part ID was already rendered
    const existingEl = renderedPartMap.get(partId);
    if (existingEl) {
      // Part already exists — update text content in-place for streaming deltas
      if (existingEl.classList.contains("part-text")) {
        // Re-render markdown HTML for streaming text updates
        existingEl.innerHTML = partEl.innerHTML;
      } else if (existingEl.classList.contains("part-thinking")) {
        // Update text content from the new element
        existingEl.textContent = partEl.textContent;
      } else if (existingEl.classList.contains("part-tool-calling")) {
        // For tool parts, replace the entire element content
        existingEl.innerHTML = partEl.innerHTML;
      }
      scrollToBottom();
      return true; // signal that we updated in-place
    }
    renderedPartMap.set(partId, partEl);
  }

  // Append inline to the last log line if it's a text part, otherwise new line
  const lastLine = outputEl.lastElementChild;
  if (
    lastLine &&
    partEl.classList.contains("part-text") &&
    lastLine.classList.contains("has-inline")
  ) {
    lastLine.appendChild(partEl);
  } else if (partEl.classList.contains("part-text")) {
    const wrapper = document.createElement("div");
    wrapper.className = "log-line has-inline";
    wrapper.appendChild(partEl);
    outputEl.appendChild(wrapper);
  } else {
    const wrapper = document.createElement("div");
    wrapper.className = "log-line";
    wrapper.appendChild(partEl);
    outputEl.appendChild(wrapper);
  }
  scrollToBottom();
  return true;
}

function scrollToBottom(): void {
  outputEl.scrollTop = outputEl.scrollHeight;
}

function setStatus(text: string, ok: boolean = true): void {
  statusEl.textContent = text;
  statusEl.className = ok ? "status-ok" : "status-error";
}

function setConnected(isConnected: boolean): void {
  connected = isConnected;
  hostInput.disabled = isConnected;
  portInput.disabled = isConnected;
  connectBtn.disabled = isConnected;
  disconnectBtn.disabled = !isConnected;
  promptInput.disabled = !isConnected;
  sendBtn.disabled = !isConnected;
  debugCheck.disabled = isConnected;

  for (const btn of [
    btnNew,
    btnAbort,
    btnStatus,
    btnSessions,
    btnMessages,
    btnClear,
    btnCompact,
    btnExport,
    btnContext,
    btnPlan,
  ]) {
    (btn as HTMLButtonElement).disabled = !isConnected;
  }
}

// ── API Actions ───────────────────────────────────────────────────

async function doConnect(): Promise<void> {
  const host = hostInput.value.trim() || "127.0.0.1";
  const port = parseInt(portInput.value.trim(), 10) || 4096;

  debugMode = debugCheck.checked;
  client = new KimixClient(host, port);

  log(`[SSE CLI] Connecting to http://${host}:${port}...`, "info");

  try {
    const healthy = await client.healthCheck();
    if (!healthy) {
      log(`[SSE CLI] Server not healthy at http://${host}:${port}`, "error");
      client = null;
      return;
    }

    session = await client.createSession("SSE Web debug session");
    log(`[SSE CLI] Created session: ${session.id}`, "info");
    sessionIdEl.textContent = session.id;
    setConnected(true);
    setStatus("Connected", true);

    if (debugMode) {
      log(`[SSE CLI] Debug mode ON`, "debug");
    }

    log(
      "[SSE CLI] Commands: /exit /new /abort /status /sessions /messages /clear /compact /export /context /plan",
      "info"
    );

    // Reset state
    renderedPartMap = new Map();
    renderedMessageIds = new Set();
    streamingParts = new Map();
    closeSSE();
    emptyPolls = 0;
    startPolling();
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`[SSE CLI] Connection failed: ${msg}`, "error");
    client = null;
    session = null;
  }
}

async function doDisconnect(): Promise<void> {
  stopPolling();
  closeSSE();
  streamingParts = new Map();
  if (client) {
    await client.deleteSession(session?.id || "").catch(() => {});
    client = null;
  }
  session = null;
  sessionIdEl.textContent = "—";
  setConnected(false);
  setStatus("Disconnected", false);
  log("[SSE CLI] Disconnected.", "info");
}

function startPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollMessages, 500);
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollMessages(): Promise<void> {
  if (!client || !session) return;

  try {
    const messages = await client.getMessages(session.id, 50);

    let newCount = 0;
    for (const msg of messages) {
      // Skip user messages: the frontend already logged the user's input manually.
      if (msg.role === "user") {
        continue;
      }
      // For cumulative backends (opencode format) skip messages we've already shown.
      if (msg.id && renderedMessageIds.has(msg.id)) {
        continue;
      }

      if (msg.id) {
        renderedMessageIds.add(msg.id);
      }

      newCount++;
      for (const part of msg.parts) {
        if (part.type === MessagePartType.STEP_START || part.type === MessagePartType.STEP_FINISH) {
          continue;
        }
        // Print context usage bar if the part type changed (mirrors _print_transition_usage)
        maybePrintContextTransition(part.type);
        // Use a composite ID for deduplication
        const pid = (part as unknown as Record<string, unknown>).id as string || `${msg.id}:${newCount}:${part.type}`;
        // Skip if already being streamed via SSE
        if (streamingParts.has(pid)) continue;
        const el = renderMessagePart(part);
        appendPart(el, pid);
      }
    }

    if (newCount === 0) {
      emptyPolls++;
      if (emptyPolls >= 4) {
        // 4 * 0.5s = 2s of no new messages
        try {
          const status = await client.getSessionStatus(session.id);
          updateContextUsage(status);
          const sessionStatus = status.type || "idle";
          if (sessionStatus === "idle" || sessionStatus === "error") {
            log(
              `[SSE CLI] Session ${sessionStatus}, stream ended. Context usage: ${formatContextUsage()}`,
              "info"
            );
            // Fetch final messages — deduplicate against already-rendered messages/parts
            const finalMessages = await client.getMessages(session.id);
            for (const msg of finalMessages) {
              if (msg.role === "user") {
                continue;
              }
              if (msg.id && renderedMessageIds.has(msg.id)) {
                continue;
              }
              if (msg.id) {
                renderedMessageIds.add(msg.id);
              }
              for (const part of msg.parts) {
                if (part.type === MessagePartType.STEP_START || part.type === MessagePartType.STEP_FINISH) {
                  continue;
                }
                const pid = (part as unknown as Record<string, unknown>).id as string || `${msg.id}:0:${part.type}`;
                // Only render if not already rendered via SSE
                if (!renderedPartMap.has(pid) && !streamingParts.has(pid)) {
                  maybePrintContextTransition(part.type);
                  const el = renderMessagePart(part);
                  appendPart(el, pid);
                }
              }
            }
            emptyPolls = 0;
            stopPolling();
            closeSSE();
          }
        } catch {
          // ignore
        }
        emptyPolls = 0;
      }
    } else {
      emptyPolls = 0;
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`[SSE CLI] get_messages error: ${msg}`, "error");
  }
}

function closeSSE(): void {
  if (sseConnection) {
    sseConnection.close();
    sseConnection = null;
  }
  streamingParts = new Map();
}

function handleSSEEvent(data: string, eventType: string): void {
  if (!client || !session) return;

  try {
    const parsed = JSON.parse(data);
    const eventTypeInner = parsed.type || eventType;

    if (eventTypeInner === "message.part.updated") {
      const partData = parsed.part || parsed.properties?.part;
      if (!partData) return;

      // Ignore events for other sessions when a sessionID is present.
      if (partData.sessionID && partData.sessionID !== session.id) {
        return;
      }

      const partId = partData.id;
      if (!partId) return;

      // Parse part data to MessagePart type
      const msgType = getMessagePartType(partData.type || "text");
      if (msgType === MessagePartType.STEP_START || msgType === MessagePartType.STEP_FINISH) {
        return;
      }

      const text = partData.text || "";

      if (msgType === MessagePartType.THINKING) {
        // Check if we already have a streaming container for this part
        let state = streamingParts.get(partId);
        if (!state) {
          // New thinking stream — finalize previous text stream, create container and append
          finalizePreviousTextStream();
          maybePrintContextTransition(MessagePartType.THINKING);

          const el = createThinkingStreamElement();
          const wrapper = document.createElement("div");
          wrapper.className = "log-line";
          wrapper.appendChild(el);
          outputEl.appendChild(wrapper);

          state = {
            element: wrapper,
            accumulatedText: text,
            type: MessagePartType.THINKING,
            partId,
            isComplete: false,
          };
          streamingParts.set(partId, state);
        } else {
          // Update existing thinking stream with new text
          state.accumulatedText = text;
          const thinkingEl = state.element?.querySelector(".part-thinking") as HTMLElement | null;
          if (thinkingEl) {
            updateThinkingStreamElement(thinkingEl, state.accumulatedText);
          }
        }
        scrollToBottom();
      } else {
        // For any non-thinking part, finalize the active thinking stream first
        finalizeActiveThinkingStream();

        if (msgType === MessagePartType.TEXT) {
          let state = streamingParts.get(partId);
          if (!state) {
            // New text stream — finalize previous text stream, create container and append
            finalizePreviousTextStream();
            maybePrintContextTransition(MessagePartType.TEXT);

            const el = createTextStreamContainer();
            const wrapper = document.createElement("div");
            wrapper.className = "log-line has-inline";
            wrapper.appendChild(el);
            outputEl.appendChild(wrapper);

            state = {
              element: wrapper,
              accumulatedText: text,
              type: MessagePartType.TEXT,
              partId,
              isComplete: false,
            };
            streamingParts.set(partId, state);
          } else {
            // Accumulate text — show raw text while streaming (before markdown rendering)
            state.accumulatedText = text;
            const textEl = state.element?.querySelector(".part-text") as HTMLElement | null;
            if (textEl) {
              textEl.textContent = state.accumulatedText;
            }
          }
          scrollToBottom();
        } else {
          // For tool and other types, render immediately (no streaming accumulation)
          maybePrintContextTransition(msgType);
          const part = {
            type: msgType,
            text: text || null,
            tool_name: partData.tool || null,
            tool_status: partData.state?.status || null,
            tool_state: partData.state || null,
            tool_result: null,
            call_id: partData.callID || null,
            reason: partData.reason || null,
            cost: partData.cost || null,
            tokens: partData.tokens || null,
            raw_data: null as Record<string, unknown> | null,
          };
          const el = renderMessagePart(part);
          appendPart(el, partId);
        }
      }
    } else if (eventTypeInner === "session.status") {
      const statusData = parsed.status || parsed.properties?.status || {};
      // Ignore status events for other sessions when a sessionID is present.
      const statusSessionId = statusData.sessionID || statusData.session_id;
      if (statusSessionId && statusSessionId !== session.id) {
        return;
      }
      const statusType = statusData.type || "";
      // Update context usage from SSE status event (backend always includes these fields)
      updateContextUsage({
        context_usage: statusData.context_usage ?? 0,
        token_count: statusData.token_count ?? 0,
      } as SessionStatus);
      if (statusType === "idle" || statusType === "error") {
        // Finalize all active streaming parts
        finalizeAllStreamingParts();
        log(`[SSE CLI] Session ${statusType}, stream ended. Context usage: ${formatContextUsage()}`, "info");
        stopPolling();
        closeSSE();
      }
    } else if (eventTypeInner === "plan.started") {
      log("[Plan] Generating plan...", "info");
    } else if (eventTypeInner === "plan.completed") {
      const planContent = parsed.properties?.planContent || "";
      const planFile = parsed.properties?.planFile || "plan.md";
      log(`[Plan] Plan saved to ${planFile}`, "info");
      // Display plan content in output area
      const planEl = document.createElement("pre");
      planEl.className = "plan-output";
      planEl.textContent = planContent;
      outputEl.appendChild(planEl);
      scrollToBottom();
    } else if (eventTypeInner === "plan.failed") {
      const error = parsed.properties?.error || "Unknown error";
      log(`[Plan] Failed: ${error}`, "error");
    }
  } catch (e) {
    if (debugMode) {
      log(`[SSE CLI] SSE parse error: ${e}`, "error");
    }
  }
}

function getMessagePartType(typeStr: string): MessagePartType {
  const map: Record<string, MessagePartType> = {
    "text": MessagePartType.TEXT,
    "reasoning": MessagePartType.THINKING,
    "tool": MessagePartType.TOOL_CALLING,
    "step-start": MessagePartType.STEP_START,
    "step-finish": MessagePartType.STEP_FINISH,
  };
  return map[typeStr] || MessagePartType.UNKNOWN;
}

async function sendPrompt(text: string): Promise<void> {
  if (!client || !session) return;

  // Check for slash commands
  if (text.startsWith("/")) {
    await handleCommand(text);
    return;
  }

  // If plan mode is pending, send as plan request instead
  if (pendingPlanMode) {
    pendingPlanMode = false;
    log(`> ${text}`, "user-input");
    log("[Plan] Generating plan...", "info");

    // Connect to SSE for real-time plan events
    closeSSE();
    emptyPolls = 0;
    sseConnection = client.streamEvents(
      (eventData, eventType) => handleSSEEvent(eventData, eventType),
      () => {
        if (debugMode) log("[SSE CLI] SSE error/close, fallback to polling", "debug");
      }
    );

    const ok = await client.sendPlan(session.id, text);
    if (!ok) {
      log("[Plan] Failed to send plan request", "error");
    }
    return;
  }

  log(`> ${text}`, "user-input");

  const ok = await client.sendPromptAsync(session.id, text);
  if (!ok) {
    log("[SSE CLI] Failed to send prompt", "error");
    return;
  }

  log("[SSE CLI] Streaming events...", "info");

  // Reset context usage tracking and streaming state for new prompt
  previousPartType = null;
  streamingParts = new Map();

  // Connect to SSE for real-time streaming
  closeSSE();
  emptyPolls = 0;
  startPolling();
  sseConnection = client.streamEvents(
    (eventData, eventType) => handleSSEEvent(eventData, eventType),
    () => {
      if (debugMode) log("[SSE CLI] SSE error/close, fallback to polling", "debug");
    }
  );
}

// ── Command Handler ───────────────────────────────────────────────

async function handleCommand(cmd: string): Promise<void> {
  if (!client || !session) return;

  const taskStr = cmd.slice(1); // remove leading /
  let taskSplit: string[];
  const splitIdx = taskStr.indexOf(":");
  if (splitIdx >= 0) {
    taskSplit = [taskStr.slice(0, splitIdx), taskStr.slice(splitIdx + 1)];
  } else {
    taskSplit = [taskStr];
  }

  const command = taskSplit[0];

  try {
    switch (command) {
      case "help":
        log(
          "[SSE CLI] Commands: /exit /new /abort /status /sessions /messages /clear /compact /export /context /plan",
          "info"
        );
        break;

      case "new":
        stopPolling();
        closeSSE();
        session = await client.createSession("SSE Web debug session");
        log(`[SSE CLI] New session: ${session.id}`, "info");
        sessionIdEl.textContent = session.id;
        emptyPolls = 0;
        renderedPartMap = new Map();
        renderedMessageIds = new Set();
        streamingParts = new Map();
        outputEl.innerHTML = "";
        previousPartType = null;
        break;

      case "abort": {
        const ok = await client.abortSession(session.id);
        log(`[SSE CLI] Abort: ${ok ? "ok" : "failed"}`, ok ? "info" : "error");
        break;
      }

      case "status": {
        const status = await client.getSessionStatus(session.id);
        log(`[SSE CLI] Status: ${JSON.stringify(status)}`, "info");
        break;
      }

      case "sessions": {
        const sessions = await client.listSessions();
        for (const s of sessions) {
          log(`  ${s.id}: ${s.title || ""}`, "info");
        }
        break;
      }

      case "messages": {
        const messages = await client.getMessages(session.id, 20);
        for (const m of messages) {
          const content = m.text_content.slice(0, 100);
          log(`  [${m.role}] ${content}...`, "info");
        }
        break;
      }

      case "clear": {
        const ok = await client.clearSession(session.id);
        log(
          `[SSE CLI] Clear: ${ok ? "ok" : "failed"}`,
          ok ? "info" : "error"
        );
        outputEl.innerHTML = "";
        renderedPartMap = new Map();
        renderedMessageIds = new Set();
        streamingParts = new Map();
        closeSSE();
        previousPartType = null;
        break;
      }

      case "compact": {
        let keep: number | undefined;
        if (taskSplit.length > 1) {
          keep = parseInt(taskSplit[1], 10);
          if (isNaN(keep)) {
            log("[SSE CLI] Usage: /compact[:N] (N = messages to keep)", "error");
            return;
          }
        }
        const ok = await client.compactSession(session.id, keep);
        log(
          `[SSE CLI] Compact: ${ok ? "ok" : "failed"}`,
          ok ? "info" : "error"
        );
        break;
      }

      case "export": {
        const outputPath = taskSplit.length > 1 ? taskSplit[1] : undefined;
        const result = await client.exportSession(session.id, outputPath);
        log(
          `[SSE CLI] Export: ${result.count || 0} messages -> ${result.output || "n/a"}`,
          "info"
        );
        break;
      }

      case "context": {
        const status = await client.getSessionStatus(session.id);
        updateContextUsage(status);
        log(`[SSE CLI] Context usage: ${formatContextUsage()}`, "info");
        break;
      }

      case "exit":
        await doDisconnect();
        break;

      case "plan": {
        log("[SSE CLI] Plan mode: Type your requirement and send it", "info");
        pendingPlanMode = true;
        break;
      }

      default:
        log(`[SSE CLI] Unrecognized command: ${command}`, "error");
        break;
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`[SSE CLI] Command failed: ${msg}`, "error");
  }
}

// ── Event Listeners ───────────────────────────────────────────────

connectBtn.addEventListener("click", () => {
  doConnect().catch(console.error);
});

disconnectBtn.addEventListener("click", () => {
  doDisconnect().catch(console.error);
});

sendBtn.addEventListener("click", () => {
  const text = promptInput.value.trim();
  if (text) {
    sendPrompt(text).catch(console.error);
    promptInput.value = "";
    promptInput.style.height = ""; // reset textarea height
  }
});

promptInput.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Enter") {
    if (e.ctrlKey || e.metaKey) {
      // Ctrl+Enter (or Cmd+Enter on Mac): insert newline at cursor
      e.preventDefault();
      const start = promptInput.selectionStart ?? 0;
      const end = promptInput.selectionEnd ?? 0;
      const val = promptInput.value;
      promptInput.value = val.slice(0, start) + "\n" + val.slice(end);
      // Move cursor after the inserted newline
      const newPos = start + 1;
      promptInput.selectionStart = promptInput.selectionEnd = newPos;
      // Trigger auto-resize
      promptInput.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    // Enter alone: send message
    e.preventDefault(); // prevent newline insertion
    const text = promptInput.value.trim();
    if (text) {
      sendPrompt(text).catch(console.error);
      promptInput.value = "";
      promptInput.style.height = ""; // reset textarea height
    }
  }
});

// Auto-resize the textarea as content grows
promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = Math.min(promptInput.scrollHeight, 300) + "px";
});

// Command button listeners
btnNew.addEventListener("click", () => handleCommand("/new"));
btnAbort.addEventListener("click", () => handleCommand("/abort"));
btnStatus.addEventListener("click", () => handleCommand("/status"));
btnSessions.addEventListener("click", () => handleCommand("/sessions"));
btnMessages.addEventListener("click", () => handleCommand("/messages"));
btnClear.addEventListener("click", () => handleCommand("/clear"));
btnCompact.addEventListener("click", () => handleCommand("/compact"));
btnExport.addEventListener("click", () => handleCommand("/export"));
btnContext.addEventListener("click", () => handleCommand("/context"));
btnPlan.addEventListener("click", () => handleCommand("/plan"));

// ── Initial State ─────────────────────────────────────────────────

setConnected(false);
log("[SSE CLI] Ready. Attempting initial connection...", "info");
doConnect().catch(console.error);
