import { renderMarkdown } from "../markdown";

// Since DOMPurify requires a browser DOM, skip tests that need DOMPurify.
// Instead, we run lightweight structural checks on the marked parser output.

function assert(condition: boolean, label: string): void {
  if (!condition) throw new Error(`FAIL: ${label}`);
  console.log(`  PASS: ${label}`);
}

// Test rendering basic markdown elements
const html = renderMarkdown("# Hello\n\nSome text.\n\n- one\n- two\n");
assert(html.includes("<h1>"), "renders h1 tags");
assert(html.includes("Hello"), "includes heading text");

// Test: Code blocks
const code = renderMarkdown("```ts\nconst x = 1;\n```");
assert(code.includes("<pre>"), "renders pre tag");
assert(code.includes("<code"), "renders code tag");
assert(code.includes("const x = 1;"), "includes code content");

// Test: GFM tables
const table = renderMarkdown("| a | b |\n|---|---|\n| c | d |\n");
assert(table.includes("<table>"), "renders table tag");

// Test: Task lists
const tasks = renderMarkdown("- [x] done\n- [ ] todo\n");
assert(tasks.includes("checkbox"), "renders checkbox input");
assert(tasks.includes("disabled"), "checkbox is disabled");

// Test: HTML sanitization (script tag stripped)
const sanitized = renderMarkdown("Hello <script>alert(1)</script> world");
assert(!sanitized.includes("<script"), "script tags stripped");
assert(!sanitized.includes("alert(1)"), "script content stripped");
assert(sanitized.includes("Hello"), "text preserved");
assert(sanitized.includes("world"), "text preserved");

// Test: External links open in new tab
const link = renderMarkdown("[link](https://example.com)");
assert(link.includes('target="_blank"'), "external link has target=_blank");
assert(link.includes('rel="noopener noreferrer"'), "external link has rel attribute");

// Test: Internal links stay same-tab
const internal = renderMarkdown("[relative](/path)");
assert(!internal.includes('target="_blank"'), "internal link has no target");

console.log("\nAll markdown tests passed.");

// Run tests
const testResults = document.getElementById("test-results");
if (testResults) {
  testResults.textContent = "All tests passed!";
}