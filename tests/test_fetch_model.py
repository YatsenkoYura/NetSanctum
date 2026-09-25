"""The model fetcher runs in a bare Alpine container, so its tools are BusyBox ones.

These tests run the real script against a stub that accepts exactly the options
BusyBox wget accepts. A flag outside that set is rejected before any connection is
made, which on a real machine is indistinguishable from a failed download: the
container exits 1 and says nothing about the cause.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_model.sh"

# BusyBox 1.36 wget, from its own usage text. Anything else is "unrecognized option".
BUSYBOX_WGET = """#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        -c|-q|-S|--spider|--header|-Y|-T|-U|-P|-O|-o) ;;
        --post-data|--post-file) ;;
        -*) echo "wget: unrecognized option: $arg" >&2; exit 1 ;;
    esac
done
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -O) out="$2"; shift 2 ;;
        -c|-q|-S) shift ;;
        http*) url="$1"; shift ;;
        *) shift ;;
    esac
done
[ -n "$SOURCE_FILE" ] || exit 1
cat "$SOURCE_FILE" > "$out"
exit 0
"""

GGUF = b"GGUF" + b"\x00" * 64
HTML = b"<!doctype html><title>404</title>"


class FetchModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.models = Path(self.tmp.name) / "models"
        self.models.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        (self.bin / "wget").write_text(BUSYBOX_WGET)
        (self.bin / "wget").chmod(0o755)

    def run_script(self, model_file="model.gguf", source=GGUF):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "MODEL_FILE": model_file,
            "MODEL_URL": "https://example.invalid/model.gguf",
            "MODELS_DIR": str(self.models),
            "SOURCE_FILE": str(Path(self.tmp.name) / "source.bin"),
        }
        Path(env["SOURCE_FILE"]).write_bytes(source)
        return subprocess.run(
            ["sh", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_a_model_is_downloaded_with_options_busybox_accepts(self):
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(GGUF, (self.models / "model.gguf").read_bytes())
        self.assertIn("model ready", result.stdout)

    def test_no_long_options_sneak_in(self):
        # The regression: `wget --show-progress` is a GNU flag, so on Alpine the
        # download aborted while parsing arguments and looked like a network fault.
        text = SCRIPT.read_text()
        self.assertNotIn("--show-progress", text)
        self.assertNotIn("--continue", text)
        for line in text.splitlines():
            if "wget" not in line or line.strip().startswith("#"):
                continue
            for flag in line.split():
                if flag.startswith("--"):
                    self.fail(f"BusyBox wget has no {flag}: {line.strip()}")

    def test_a_response_that_is_not_a_model_is_refused(self):
        result = self.run_script(source=HTML)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not a GGUF model", result.stderr)
        # A refused download must not leave a file the next run would trust.
        self.assertFalse((self.models / "model.gguf").exists())
        self.assertFalse((self.models / "model.gguf.part").exists())

    def test_missing_settings_are_reported_by_name(self):
        result = subprocess.run(
            ["sh", str(SCRIPT)],
            env={"PATH": "/usr/bin:/bin", "MODELS_DIR": str(self.models)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("MIKU_MODEL_FILE", result.stderr)

    def test_an_existing_model_is_left_alone(self):
        (self.models / "model.gguf").write_bytes(GGUF)
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("already present", result.stdout)


if __name__ == "__main__":
    unittest.main()
