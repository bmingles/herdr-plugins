/**
 * Just enough JSONC to read `config.example.json`-shaped files: strip line comments
 * and block comments (respecting string literals) and trailing commas, then
 * `JSON.parse`. Not a
 * general-purpose parser -- a small inline stripper, matching the philosophy of
 * agent-caffeinate's `src/jsonc.py` rather than pulling in a dependency for this.
 */
export function parseJsonc(text: string): unknown {
  let out = "";
  let inString = false;
  let inLineComment = false;
  let inBlockComment = false;
  let escaped = false;

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const next = text[i + 1];

    if (inLineComment) {
      if (c === "\n") {
        inLineComment = false;
        out += c;
      }
      continue;
    }
    if (inBlockComment) {
      if (c === "*" && next === "/") {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      out += c;
      if (escaped) {
        escaped = false;
      } else if (c === "\\") {
        escaped = true;
      } else if (c === '"') {
        inString = false;
      }
      continue;
    }

    if (c === '"') {
      inString = true;
      out += c;
      continue;
    }
    if (c === "/" && next === "/") {
      inLineComment = true;
      i++;
      continue;
    }
    if (c === "/" && next === "*") {
      inBlockComment = true;
      i++;
      continue;
    }
    out += c;
  }

  // Trailing commas before `}` or `]`.
  out = out.replace(/,(\s*[}\]])/g, "$1");

  return JSON.parse(out);
}
