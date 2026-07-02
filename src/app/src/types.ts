// types.ts — TypeScript types mirroring Python dataclasses in client.py

export enum MessagePartType {
  TEXT = "text",
  THINKING = "thinking",
  TOOL_CALLING = "tool_calling",
  TOOL_CALLING_PART = "tool_calling_part",
  TOOL_RESULT = "tool_result",
  STEP_START = "step-start",
  STEP_FINISH = "step-finish",
  UNKNOWN = "unknown",
}

export interface MessagePart {
  type: MessagePartType;
  text: string | null;
  tool_name: string | null;
  tool_status: string | null;
  tool_state: Record<string, unknown> | null;
  tool_result: string | null;
  call_id: string | null;
  reason: string | null;
  cost: number | null;
  tokens: Record<string, unknown> | null;
  raw_data: Record<string, unknown> | null;
}

export interface Message {
  id: string;
  role: string;
  parts: MessagePart[];
  created_at: number | null;
  text_content: string;
}

export interface Session {
  id: string;
  title: string | null;
  created_at: number | null;
  updated_at: number | null;
  parent_id: string | null;
}

export interface SessionStatus {
  type: string; // "idle" | "busy" | "error"
  time: number;
  token_count?: number;
  context_usage?: number;
}

// SSE Event (raw)
export interface RawSSEEvent {
  event: string;
  data: string;
  id: string | null;
}

// OpenCode wire format type mapping
const OPENCODE_TYPE_MAP: Record<string, MessagePartType> = {
  tool: MessagePartType.TOOL_CALLING,
  reasoning: MessagePartType.THINKING,
};

// Dummy session manager type mapping
const DUMMY_TYPE_MAP: Record<string, MessagePartType> = {
  Text: MessagePartType.TEXT,
  Thinking: MessagePartType.THINKING,
  ToolCalling: MessagePartType.TOOL_CALLING,
  ToolCallingPart: MessagePartType.TOOL_CALLING_PART,
  ToolResult: MessagePartType.TOOL_RESULT,
};

export function parseMessagePart(data: Record<string, unknown>): MessagePart {
  let partType = (data.type as string) || "text";
  partType = OPENCODE_TYPE_MAP[partType] || partType;
  const msgType = Object.values(MessagePartType).includes(partType as MessagePartType)
    ? (partType as MessagePartType)
    : MessagePartType.UNKNOWN;

  if (msgType === MessagePartType.UNKNOWN) {
    return { type: MessagePartType.UNKNOWN, raw_data: data } as unknown as MessagePart;
  }

  const part: MessagePart = {
    type: msgType,
    text: null,
    tool_name: null,
    tool_status: null,
    tool_state: null,
    tool_result: null,
    call_id: null,
    reason: null,
    cost: null,
    tokens: null,
    raw_data: null,
  };

  if (msgType === MessagePartType.TEXT) {
    part.text = (data.text as string) || null;
  } else if (msgType === MessagePartType.TOOL_CALLING) {
    part.tool_name = (data.tool as string) || null;
    part.call_id = (data.callID as string) || null;
    const state = (data.state as Record<string, unknown>) || {};
    part.tool_status = (state.status as string) || null;
    part.tool_state = state;
  } else if (msgType === MessagePartType.THINKING) {
    part.text = (data.text as string) || null;
  } else if (msgType === MessagePartType.TOOL_CALLING_PART || msgType === MessagePartType.TOOL_RESULT) {
    part.text = (data.text as string) || null;
  } else if (msgType === MessagePartType.STEP_FINISH) {
    part.reason = (data.reason as string) || null;
    part.cost = (data.cost as number) || null;
    part.tokens = (data.tokens as Record<string, unknown>) || null;
  }

  return part;
}

export function parseMessage(data: Record<string, unknown>): Message {
  // Handle dummy session manager format: { "type": "Text|Thinking|ToolCalling", "text": "...", "time": ts }
  if (!data.info) {
    const msgType = (data.type as string) || "Text";
    const text = (data.text as string) || "";
    const created_at = (data.time as number) || null;
    const partType = DUMMY_TYPE_MAP[msgType] || MessagePartType.UNKNOWN;
    const part: MessagePart = {
      type: partType,
      text: text,
      tool_name: null,
      tool_status: null,
      tool_state: null,
      tool_result: null,
      call_id: null,
      reason: null,
      cost: null,
      tokens: null,
      raw_data: null,
    };
    if (data.tool_name) part.tool_name = data.tool_name as string;
    if (data.tool_result) part.tool_result = data.tool_result as string;

    return {
      id: "",
      role: "assistant",
      parts: [part],
      created_at,
      text_content: text,
    };
  }

  // Opencode format: { "info": {...}, "parts": [...] }
  const info = data.info as Record<string, unknown>;
  const timeInfo = (info.time as Record<string, unknown>) || {};
  const parts = (data.parts as Array<Record<string, unknown>>) || [];
  const msgParts = parts.map(parseMessagePart);

  return {
    id: (info.id as string) || "",
    role: (info.role as string) || "assistant",
    parts: msgParts,
    created_at: (timeInfo.created as number) || null,
    text_content: msgParts
      .filter(p => p.type === MessagePartType.TEXT && p.text)
      .map(p => p.text)
      .join(""),
  };
}

export function parseSession(data: Record<string, unknown>): Session {
  return {
    id: (data.id as string) || "",
    title: (data.title as string) || null,
    created_at: (data.createdAt as number) || null,
    updated_at: (data.updatedAt as number) || null,
    parent_id: (data.parentID as string) || null,
  };
}
