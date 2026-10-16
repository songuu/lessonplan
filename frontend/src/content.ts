import DOMPurify from "dompurify";
import { marked } from "marked";
import katex from "katex";
export function renderMarkdown(markdown: string) {
  const equations: string[] = [];
  const safe = markdown.replace(
    /\$\$([\s\S]+?)\$\$|(?<!\\)\$([^$\n]+?)\$/g,
    (source, display, inline) => {
      try {
        const result = katex.renderToString(display || inline, {
          displayMode: !!display,
          throwOnError: true,
          trust: false,
          strict: "ignore",
        });
        equations.push(result);
        return `LESSONMATHPLACEHOLDER${equations.length - 1}END`;
      } catch {
        return source;
      }
    },
  );
  let html = marked.parse(safe, { async: false, gfm: true }) as string;
  html = DOMPurify.sanitize(html);
  // Formula HTML crosses the same sanitizer boundary as teacher-authored Markdown.
  return DOMPurify.sanitize(
    html.replace(
      /LESSONMATHPLACEHOLDER(\d+)END/g,
      (_, id) => equations[Number(id)] || "",
    ),
  );
}
export function locateSelection(markdown: string, selected: string) {
  if (!selected) return null;
  const start = markdown.indexOf(selected);
  if (start < 0 || markdown.indexOf(selected, start + 1) >= 0) return null;
  return { start, end: start + selected.length, text: selected };
}
export function applySelectionCandidate(
  base: string,
  current: string,
  range: { start: number; end: number; text: string },
  replacement: string,
) {
  if (base !== current || base.slice(range.start, range.end) !== range.text)
    throw new Error("润色期间正文已修改，请重新选择后润色。");
  return base.slice(0, range.start) + replacement + base.slice(range.end);
}
