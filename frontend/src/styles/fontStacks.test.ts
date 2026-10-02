import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(__dirname, "..");

// A quoted generic family is a font *named* "sans-serif", not the generic one.
// --font-family ended in 'sans-serif', so on machines with none of the named
// fonts (most Linux desktops, and the docs screenshot runner) everything using
// it fell back to the browser's default serif font. Keywords are
// case-insensitive, so 'Sans-Serif' is the same bug.
const QUOTED_GENERIC =
  /["'](serif|sans-serif|monospace|cursive|fantasy|system-ui|ui-serif|ui-sans-serif|ui-monospace|ui-rounded|math|emoji|fangsong)["']/gi;

describe("CSS never quotes a generic font family", () => {
  const files = readdirSync(SRC, { recursive: true, encoding: "utf8" })
    .map((f) => f.replaceAll("\\", "/"))
    .filter((f) => f.endsWith(".css"));

  it("finds the stylesheets, nested ones included", () => {
    expect(files).toEqual(expect.arrayContaining(["index.css", "styles/projectMgmt.css"]));
  });

  it.each(files)("%s", (rel) => {
    expect(readFileSync(join(SRC, rel), "utf8").match(QUOTED_GENERIC)).toBeNull();
  });
});
