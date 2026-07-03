// renderer.ts — Message rendering mirroring _print_message_part from sse_cli.py

import { MessagePartType } from "./types";
import type { MessagePart } from "./types";
import { renderMarkdown } from "./markdown";

/** Truncate long strings, keeping head and tail. */
export function fmtArg(s: string, maxLen: number = 120): string {
  if (s.length <= maxLen) return s;
  const head = Math.floor(maxLen / 2);
  const tail = maxLen - head - 3;
  return s.slice(0, head) + "..." + s.slice(-tail);
}

/** Format unix timestamp to HH:MM:SS. */
export function fmtTs(unixT: number | null): string {
  if (!unixT) return "";
  return new Date(unixT * 1000).toLocaleTimeString("en-US", { hour12: false });
}

/** Map MessagePartType to CSS class name. */
function partTypeClass(type: MessagePartType): string {
  switch (type) {
    case MessagePartType.TEXT:
      return "part-text";
    case MessagePartType.THINKING:
      return "part-thinking";
    case MessagePartType.TOOL_CALLING:
      return "part-tool-calling";
    case MessagePartType.TOOL_CALLING_PART:
      return "part-tool-calling-part";
    case MessagePartType.TOOL_RESULT:
      return "part-tool-result";
    default:
      return "part-unknown";
  }
}

/** Render a single MessagePart to an HTML element. */
export function renderMessagePart(part: MessagePart): HTMLElement {
  const el = document.createElement("span");
  el.className = `msg-part ${partTypeClass(part.type)}`;

  switch (part.type) {
    case MessagePartType.TEXT:
      if (part.text) {
        el.innerHTML = renderMarkdown(part.text);
      }
      break;

    case MessagePartType.THINKING:
      if (part.text) {
        el.textContent = part.text;
      }
      break;

    case MessagePartType.TOOL_CALLING: {
      const toolName = part.tool_name || "unknown";
      const args = part.text ? ` ${part.text}` : "";

      const header = document.createElement("div");
      header.className = "tool-header";
      header.textContent = `⚡ ${toolName}${args}`;
      el.appendChild(header);
      break;
    }

    case MessagePartType.TOOL_CALLING_PART:
      if (part.text) {
        el.textContent = part.text;
      }
      break;

    case MessagePartType.TOOL_RESULT: {
      const resultText = part.tool_result || part.text || "";
      if (resultText) {
        const state = part.tool_state || {};
        const prefix = state.error ? "✗ " : "✓ ";
        el.textContent = `${prefix}${resultText}`;
        if (state.error) {
          el.classList.add("tool-error");
        }
      }
      break;
    }

    default:
      el.textContent = JSON.stringify(part.raw_data || part);
      break;
  }

  return el;
}

// ── Streaming Render Functions ────────────────────────────────────

/** Create an empty element ready for streaming thinking content. */
export function createThinkingStreamElement(): HTMLElement {
  const el = document.createElement("span");
  el.className = "msg-part part-thinking";
  el.dataset.streaming = "true";
  return el;
}

/** Update a thinking stream element with new text content (realtime). */
export function updateThinkingStreamElement(el: HTMLElement, text: string): void {
  el.textContent = text;
}

/** Create an element for accumulating text that will be markdown-rendered later. */
export function createTextStreamContainer(): HTMLElement {
  const el = document.createElement("span");
  el.className = "msg-part part-text";
  el.dataset.streaming = "true";
  return el;
}

/** Finalize a text stream: render accumulated text as markdown. */
export function finalizeTextStreamElement(el: HTMLElement, text: string): void {
  el.innerHTML = renderMarkdown(text);
  delete el.dataset.streaming;
}
