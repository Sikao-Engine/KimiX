// parse_test.ts — Test the message parsing logic against both wire formats

// Simulate the types module inline for testing
enum MessagePartType {
  TEXT = "text",
  THINKING = "thinking",
  TOOL_CALLING = "tool_calling",
  TOOL_CALLING_PART = "tool_calling_part",
  TOOL_RESULT = "tool_result",
  STEP_START = "step-start",
  STEP_FINISH = "step-finish",
  UNKNOWN = "unknown",
}

interface MessagePart {
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

interface Message {
  id: string;
  role: string;
  parts: MessagePart[];
  created_at: number | null;
  text_content: string;
}

// ── Mirrors types.ts ─────────────────────────────────────────────

const OPENCODE_TYPE_MAP: Record<string, MessagePartType> = {
  tool: MessagePartType.TOOL_CALLING,
  reasoning: MessagePartType.THINKING,
};

const DUMMY_TYPE_MAP: Record<string, MessagePartType> = {
  Text: MessagePartType.TEXT,
  Thinking: MessagePartType.THINKING,
  ToolCalling: MessagePartType.TOOL_CALLING,
  ToolCallingPart: MessagePartType.TOOL_CALLING_PART,
  ToolResult: MessagePartType.TOOL_RESULT,
};

function parseMessagePart(data: Record<string, unknown>): MessagePart {
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

function parseMessage(data: Record<string, unknown>): Message {
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

// ── Helper to mimic renderMessagePart logic ──────────────────────

function renderText(part: MessagePart): string {
  switch (part.type) {
    case MessagePartType.TEXT:
      return part.text ? `[TEXT] ${part.text}` : "[TEXT] <empty/null>";
    case MessagePartType.THINKING:
      return part.text ? `[THINK] ${part.text}` : "[THINK] <empty/null>";
    case MessagePartType.TOOL_CALLING:
      return `[TOOL] ${part.tool_name} status=${part.tool_status}`;
    case MessagePartType.TOOL_CALLING_PART:
      return `[TOOL_PART] ${part.text}`;
    case MessagePartType.TOOL_RESULT:
      return `[TOOL_RESULT] ${part.tool_result || part.text}`;
    case MessagePartType.STEP_START:
      return "[STEP_START]";
    case MessagePartType.STEP_FINISH:
      return `[STEP_FINISH] reason=${part.reason}`;
    default:
      return `[UNKNOWN] ${JSON.stringify(part.raw_data)}`;
  }
}

// ── Tests ────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

let assertIdx = 0;
function assert(condition: boolean, label: string): void {
  assertIdx++;
  if (condition) {
    passed++;
    console.log(`  [PASS #${assertIdx}] ${label}`);
  } else {
    failed++;
    console.log(`  [FAIL #${assertIdx}] ${label}`);
  }
}

// Test 1: Dummy Text message
{
  const data = { type: "Text", text: "Hello world", time: 1234567890.123 };
  const msg = parseMessage(data);
  console.log("Test 1: Dummy Text message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TEXT, "part type is TEXT");
  assert(msg.parts[0].text === "Hello world", "part text is correct");
  assert(renderText(msg.parts[0]) === "[TEXT] Hello world", "renders correctly");
}

// Test 2: Dummy Thinking message
{
  const data = { type: "Thinking", text: "Hmm, let me think...", time: 1234567890.123 };
  const msg = parseMessage(data);
  console.log("Test 2: Dummy Thinking message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.THINKING, "part type is THINKING");
  assert(msg.parts[0].text === "Hmm, let me think...", "part text is correct");
  assert(renderText(msg.parts[0]) === "[THINK] Hmm, let me think...", "renders correctly");
}

// Test 3: Dummy ToolCalling message
{
  const data = { type: "ToolCalling", text: "read {\"path\": \"/foo\"}", time: 1234567890.123, tool_name: "read" };
  const msg = parseMessage(data);
  console.log("Test 3: Dummy ToolCalling message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TOOL_CALLING, "part type is TOOL_CALLING");
  assert(msg.parts[0].tool_name === "read", "tool_name is correct");
  assert(renderText(msg.parts[0]) === "[TOOL] read status=unknown", "renders correctly");
}

// Test 4: Dummy ToolResult message
{
  const data = { type: "ToolResult", text: "[ToolResult] file contents here", time: 1234567890.123, tool_result: "file contents here" };
  const msg = parseMessage(data);
  console.log("Test 4: Dummy ToolResult message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TOOL_RESULT, "part type is TOOL_RESULT");
  assert(msg.parts[0].tool_result === "file contents here", "tool_result is correct");
  assert(renderText(msg.parts[0]) === "[TOOL_RESULT] file contents here", "renders correctly");
}

// Test 5: Opencode Text part
{
  const data = {
    info: { id: "msg_001", role: "assistant", time: { created: 1234567890000 } },
    parts: [
      { id: "prt_001", type: "text", text: "Hello from opencode", sessionID: "ses_001", messageID: "msg_001" }
    ]
  };
  const msg = parseMessage(data);
  console.log("Test 5: Opencode Text message");
  assert(msg.id === "msg_001", "message id correct");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TEXT, "part type is TEXT");
  assert(msg.parts[0].text === "Hello from opencode", "part text is correct");
  assert(msg.text_content === "Hello from opencode", "text_content is correct");
  assert(renderText(msg.parts[0]) === "[TEXT] Hello from opencode", "renders correctly");
}

// Test 6: Opencode Tool part (type="tool" → mapped to tool_calling)
{
  const data = {
    info: { id: "msg_002", role: "assistant", time: { created: 1234567890000 } },
    parts: [
      { id: "prt_002", type: "tool", tool: "read", callID: "toolu_001", sessionID: "ses_001", messageID: "msg_002", state: { status: "running", input: { path: "/foo" } } }
    ]
  };
  const msg = parseMessage(data);
  console.log("Test 6: Opencode Tool message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TOOL_CALLING, "part type is TOOL_CALLING (mapped from tool)");
  assert(msg.parts[0].tool_name === "read", "tool_name is correct");
  assert(msg.parts[0].call_id === "toolu_001", "call_id is correct");
  assert(msg.parts[0].tool_status === "running", "tool_status is correct");
}

// Test 7: Opencode Reasoning part (type="reasoning" → mapped to thinking)
{
  const data = {
    info: { id: "msg_003", role: "assistant", time: { created: 1234567890000 } },
    parts: [
      { id: "prt_003", type: "reasoning", text: "Let me analyze this...", sessionID: "ses_001", messageID: "msg_003" }
    ]
  };
  const msg = parseMessage(data);
  console.log("Test 7: Opencode Reasoning message");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.THINKING, "part type is THINKING (mapped from reasoning)");
  assert(msg.parts[0].text === "Let me analyze this...", "part text is correct");
}

// Test 8: Opencode Step-Start/Step-Finish
{
  const data = {
    info: { id: "msg_004", role: "assistant", time: { created: 1234567890000 } },
    parts: [
      { id: "prt_004", type: "step-start", sessionID: "ses_001", messageID: "msg_004", snapshot: "abc123" },
      { id: "prt_005", type: "step-finish", sessionID: "ses_001", messageID: "msg_004", reason: "stop", cost: 0.05, tokens: { input: 100, output: 50 } }
    ]
  };
  const msg = parseMessage(data);
  console.log("Test 8: Opencode Step-Start/Finish");
  assert(msg.parts.length === 2, "has 2 parts");
  assert(msg.parts[0].type === MessagePartType.STEP_START, "first part is STEP_START");
  assert(msg.parts[1].type === MessagePartType.STEP_FINISH, "second part is STEP_FINISH");
  assert(msg.parts[1].reason === "stop", "reason is correct");
  assert(msg.parts[1].cost === 0.05, "cost is correct");
}

// Test 9: Empty dummy data (edge case)
{
  const data = {} as Record<string, unknown>;
  const msg = parseMessage(data);
  console.log("Test 9: Empty dummy data");
  assert(msg.parts.length === 1, "has 1 part");
  assert(msg.parts[0].type === MessagePartType.TEXT, "defaults to TEXT");
  assert(msg.parts[0].text === "", "defaults to empty text");
  assert(renderText(msg.parts[0]) === "[TEXT] <empty/null>", "renders as empty");
}

// Test 10: Dummy type that doesn't exist in DUMMY_TYPE_MAP
{
  const data = { type: "UnknownType", text: "something" };
  const msg = parseMessage(data);
  console.log("Test 10: Unknown dummy type");
  assert(msg.parts[0].type === MessagePartType.UNKNOWN, "falls back to UNKNOWN");
  assert(renderText(msg.parts[0]).includes("[UNKNOWN]"), "renders as unknown");
}

// Test 11: Multi-part opencode message (text + tool + step-finish)
{
  const data = {
    info: { id: "msg_005", role: "assistant", time: { created: 1234567890000 } },
    parts: [
      { id: "prt_006", type: "text", text: "I'll read the file.", sessionID: "ses_001", messageID: "msg_005" },
      { id: "prt_007", type: "tool", tool: "read", callID: "toolu_002", sessionID: "ses_001", messageID: "msg_005", state: { status: "completed", output: "file contents" } },
      { id: "prt_008", type: "step-finish", reason: "stop", cost: 0.03, tokens: { input: 50, output: 30 } }
    ]
  };
  const msg = parseMessage(data);
  console.log("Test 11: Multi-part opencode message");
  assert(msg.parts.length === 3, "has 3 parts");
  assert(msg.parts[0].type === MessagePartType.TEXT && msg.parts[0].text === "I'll read the file.", "part 0: TEXT");
  assert(msg.parts[1].type === MessagePartType.TOOL_CALLING && msg.parts[1].tool_name === "read", "part 1: TOOL_CALLING");
  assert(msg.parts[2].type === MessagePartType.STEP_FINISH, "part 2: STEP_FINISH");
}

// ── Summary ──────────────────────────────────────────────────────

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  throw new Error(`${failed} test(s) failed`);
}
