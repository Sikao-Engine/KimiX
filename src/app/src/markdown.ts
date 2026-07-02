import { Marked } from "marked";
import DOMPurify from "dompurify";

export interface RenderMarkdownOptions {
  /** Open external links in a new tab with noopener/noreferrer. */
  openLinksInNewTab?: boolean;
}

const EXTERNAL_LINK_RE = /^https?:\/\//i;

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const md = new Marked({ gfm: true });

md.use({
  renderer: {
    link({ href, title, text }) {
      const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
      if (EXTERNAL_LINK_RE.test(href ?? "")) {
        return `<a href="${escapeHtml(href ?? "#")}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
      }
      return `<a href="${escapeHtml(href ?? "#")}"${titleAttr}>${text}</a>`;
    },
  },
});

const ALLOWED_TAGS = [
  "p", "br", "hr",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li",
  "strong", "em", "del", "s", "b", "i",
  "a", "code", "pre", "blockquote",
  "table", "thead", "tbody", "tr", "th", "td",
  "input", "div", "span", "sup", "sub",
];

const ALLOWED_ATTR = [
  "href", "title", "target", "rel", "class", "start", "type", "checked", "disabled",
];

export function renderMarkdown(
  text: string,
  options: RenderMarkdownOptions = {},
): string {
  const rawHtml = md.parse(text, { async: false }) as string;
  const openLinks = options.openLinksInNewTab ?? true;

  const purifyConfig = {
    ALLOWED_TAGS,
    ALLOWED_ATTR: openLinks
      ? ALLOWED_ATTR
      : ALLOWED_ATTR.filter((a) => a !== "target" && a !== "rel"),
    ALLOW_DATA_ATTR: false,
    SANITIZE_DOM: true,
    ALLOWED_URI_REGEXP:
      /^(?:(?:(?:f|ht)tps?|file|mailto|vscode|vscode-insiders):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
  };

  return DOMPurify.sanitize(rawHtml, purifyConfig) as string;
}