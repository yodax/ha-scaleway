#!/usr/bin/env node
/**
 * Validate Home Assistant translation strings as ICU MessageFormat.
 *
 * HA's frontend parses every value in strings.json / translations/*.json with
 * @formatjs/icu-messageformat-parser, the same parser used here. In that
 * syntax `<word>` opens a rich-text tag and `{word}` is a placeholder, so
 * prose like "<name>.tar" is read as an unclosed tag and the UI renders
 * "translation error: UNCLOSED_TAG" instead of the string.
 *
 * Nothing else catches this: the files are valid JSON and the integration
 * imports fine, so the failure only ever appears in the browser.
 *
 * Usage: node scripts/validate_translations.js <file...>
 */
const { parse } = require("@formatjs/icu-messageformat-parser");
const fs = require("fs");

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error("usage: node scripts/validate_translations.js <file...>");
  process.exit(2);
}

let checked = 0;
const failures = [];

function walk(node, path, file) {
  for (const [key, value] of Object.entries(node)) {
    const full = path ? `${path}.${key}` : key;
    if (typeof value === "string") {
      checked++;
      try {
        parse(value);
      } catch (err) {
        failures.push({
          file,
          path: full,
          message: `${err.name || "Error"}: ${String(err.message).split("\n")[0]}`,
          text: value,
        });
      }
    } else if (value && typeof value === "object") {
      walk(value, full, file);
    }
  }
}

for (const file of files) {
  walk(JSON.parse(fs.readFileSync(file, "utf8")), "", file);
}

console.log(`Parsed ${checked} strings across ${files.length} file(s).`);

if (failures.length > 0) {
  console.error(`\n${failures.length} string(s) failed to parse:\n`);
  for (const f of failures) {
    console.error(`  ${f.file}`);
    console.error(`    key  : ${f.path}`);
    console.error(`    error: ${f.message}`);
    console.error(`    text : ${f.text.slice(0, 160)}\n`);
  }
  process.exit(1);
}

console.log("All strings parse cleanly.");
