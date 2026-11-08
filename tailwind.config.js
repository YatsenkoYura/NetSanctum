const fs = require("node:fs");
const path = require("node:path");

// Source of truth is the filesystem, but the committed CSS must stay
// canonical: git-ignored local modules and files are skipped, so a local
// build produces the same output as CI (which has no ignored trees at
// all). Removing a folder drops it from the scan automatically — no
// per-module exception list. Tests are excluded: they are not shipped
// templates and their assertions may name classes nothing renders.
const ignored = new Set();
if (fs.existsSync(".gitignore")) {
  for (const line of fs.readFileSync(".gitignore", "utf8").split("\n")) {
    const entry = line.trim();
    if (entry.startsWith("app/") && !entry.startsWith("!")) ignored.add(entry.replace(/\/$/, ""));
  }
}
const isIgnored = (p) => {
  const normalized = p.split(path.sep).join("/");
  for (const prefix of ignored) {
    if (normalized === prefix || normalized.startsWith(prefix + "/")) return true;
  }
  return false;
};
const content = [];
(function collect(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (isIgnored(full)) continue;
    if (entry.isDirectory()) {
      if (entry.name !== "__pycache__") collect(full);
    } else if ((full.endsWith(".html") || full.endsWith(".py")) && !full.includes("/tests/")) {
      content.push(full);
    }
  }
})("app");

content.push("static/browser-runtime.js", "static/miku-assistant.js", "static/miku-dashboard.js");

/** @type {import('tailwindcss').Config} */
module.exports = {
  content,
  safelist: [
    "hidden",
    "flex",
    "opacity-45",
    "sm:col-span-2",
    "lg:col-span-2",
    "border-red-500",
    "border-teal-400",
    "text-red-400",
    "text-red-500",
    "text-teal-400",
    "text-zinc-600",
    "hover:text-zinc-400",
    "border-zinc-800",
    "hover:border-zinc-700",
    "bg-zinc-900/55",
    "bg-black",
    "bg-emerald-950/40",
    "text-emerald-400",
    "border-emerald-800/80",
    "bg-amber-950/40",
    "text-amber-400",
    "border-amber-800/80",
    "border-amber-400",
    "bg-red-950/40",
    "border-red-800/80",
    "border-red-400",
    "bg-yellow-950/40",
    "text-yellow-400",
    "border-yellow-800/80",
    "border-purple-400",
    "text-purple-400",
    "border-blue-400",
    "text-blue-400",
    "border-green-400",
    "text-green-400",
    "bg-emerald-400",
    "bg-amber-400",
    "bg-red-500",
    "bg-rose-500",
    "text-emerald-500",
    "border-rose-400",
    "hover:bg-rose-400",
    "hover:text-black",
    "text-rose-400"
  ],
  theme: {
    extend: {
      // Future global themes: semantic tokens instead of raw palette names.
      // Templates use bg-panel/text-muted/border-line/text-accent and a
      // theme is just a different static/themes/*.css assigning the
      // --ns-* variables. Values in dark.css match the current zinc/teal
      // look, so wiring a theme alters no layout. Not wired into base.html
      // yet — raw zinc/teal classes keep working.
      colors: {
        panel: "var(--ns-panel)",
        surface: "var(--ns-surface)",
        muted: "var(--ns-muted)",
        accent: "var(--ns-accent)",
        line: "var(--ns-line)"
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', "monospace"],
        sans: ['"Inter"', "system-ui", "sans-serif"]
      }
    }
  },
  plugins: []
};
