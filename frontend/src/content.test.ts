import { describe, it, expect } from "vitest";
import {
  applySelectionCandidate,
  locateSelection,
  renderMarkdown,
} from "./content";
import { parseFrame } from "./api";
describe("teacher content boundary", () => {
  it("removes script and dangerous links but renders core formulas", () => {
    const html = renderMarkdown(
      "<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n$E=mc^2$",
    );
    expect(html).not.toContain("<script>");
    expect(html).not.toContain('href="javascript:');
    expect(html).toContain("katex");
  });
  it("keeps invalid formula source visible", () =>
    expect(renderMarkdown("$\\unknown{x}$")).toContain("unknown"));
  it("rejects ambiguous selection and changed working copies", () => {
    expect(locateSelection("甲乙甲", "甲")).toBeNull();
    expect(() =>
      applySelectionCandidate(
        "甲乙",
        "甲丙",
        { start: 1, end: 2, text: "乙" },
        "丁",
      ),
    ).toThrow();
    expect(
      applySelectionCandidate(
        "甲乙",
        "甲乙",
        { start: 1, end: 2, text: "乙" },
        "丁",
      ),
    ).toBe("甲丁");
  });
  it("parses replay event IDs and wrapped payloads", () => {
    expect(
      parseFrame(
        'id: 7\r\nevent: content_delta\r\ndata: {"payload":{"delta":"教学"}}',
      ),
    ).toMatchObject({
      id: "7",
      event: "content_delta",
      data: { delta: "教学" },
    });
    expect(parseFrame(": heartbeat")).toBeNull();
  });
});
