import { readFileSync } from "node:fs";
import { join } from "node:path";

// The app is always dark, but these shared modal styles (used by the Create PR
// Campaign and Import Existing modals) switched to dark only under the OS's
// prefers-color-scheme. On a machine in light mode the header and footer came
// out near-white behind the app's near-white title text, so the title vanished.
const css = readFileSync(join(__dirname, "TemplateModal.css"), "utf8");

// The body of the top-level rule for exactly this selector (not, say,
// `.modal-header h3`), found by plain search rather than a regex.
function ruleBody(selector: string): string {
  const start = css.indexOf(`\n${selector} {`);
  if (start === -1) throw new TypeError(`no ${selector} rule in TemplateModal.css`);
  const open = css.indexOf("{", start);
  return css.slice(open + 1, css.indexOf("}", open));
}

describe("shared modal styles follow the app's dark theme", () => {
  it("never depends on the OS colour scheme", () => {
    expect(css).not.toMatch(/prefers-color-scheme/);
  });

  it.each([".modal-header", ".modal-footer"])("%s has a dark background", (selector) => {
    expect(ruleBody(selector)).toMatch(/background:\s*#1a202c;/);
  });
});
