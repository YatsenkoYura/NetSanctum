const { execFileSync } = require("node:child_process");

const content = execFileSync(
  "git",
  ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "app"],
  { encoding: "utf8" }
)
  .split("\0")
  .filter((path) => path.endsWith(".html") || path.endsWith(".py"));

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
    "text-emerald-500"
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', "monospace"],
        sans: ['"Inter"', "system-ui", "sans-serif"]
      }
    }
  },
  plugins: []
};
