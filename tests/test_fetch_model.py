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
# The source is whatever a real server would have sent for that url.
src="$SRC_DIR/${url##*/}"
[ -f "$src" ] || exit 1
cat "$src" > "$out"
exit 0
"""

GGUF = b"GGUF" + b"\x00" * 64
HTML = b"<!doctype html><title>404</title>"
# An ONNX model is a protobuf whose first field is the IR version, and a TorchScript
# checkpoint is a zip archive, which is also what a plain zip looks like.
ONNX = b"\x08\x07" + b"\x12" * 62
ZIP = b"PK\x03\x04" + b"\x00" * 62
# whisper.cpp still ships the older ggml container, not GGUF.
GGML = b"lmgg" + b"\x00" * 64


class FetchModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.models = Path(self.tmp.name) / "models"
        self.models.mkdir()
        self.sources = Path(self.tmp.name) / "sources"
        self.sources.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        (self.bin / "wget").write_text(BUSYBOX_WGET)
        (self.bin / "wget").chmod(0o755)

    def serve(self, name: str, payload: bytes) -> str:
        """Put a file where the stub server will find it for that url."""
        (self.sources / name).write_bytes(payload)
        return f"https://example.invalid/{name}"

    def run_script(self, model_file="model.gguf", source=GGUF):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "MODEL_FILE": model_file,
            "MODEL_URL": self.serve("model.gguf", source),
            "MODELS_DIR": str(self.models),
            "SRC_DIR": str(self.sources),
        }
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
        self.assertIn("not a GGUF file", result.stderr)
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

    def run_manifest(self, manifest: str, sources: dict[str, bytes]):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "MODELS_DIR": str(self.models),
            "SRC_DIR": str(self.sources),
            "VOICE_MANIFEST": manifest,
        }
        for name, payload in sources.items():
            self.serve(name, payload)
        return subprocess.run(
            ["sh", str(SCRIPT), "--manifest"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_a_manifest_fetches_every_kind_of_model(self):
        # The voice stack is four different containers from four different places; the
        # fetcher has to accept each of them and reject an error page for each.
        result = self.run_manifest(
            "base.bin|https://example.invalid/base.bin|GGML\n"
            "llama.gguf|https://example.invalid/llama.gguf|GGUF\n"
            "model.onnx|https://example.invalid/model.onnx|ONNX\n"
            "voice.pt|https://example.invalid/voice.pt|ZIP\n",
            {"base.bin": GGML, "llama.gguf": GGUF, "model.onnx": ONNX, "voice.pt": ZIP},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for name, payload in (
            ("base.bin", GGML),
            ("llama.gguf", GGUF),
            ("model.onnx", ONNX),
            ("voice.pt", ZIP),
        ):
            self.assertEqual(payload, (self.models / name).read_bytes(), name)

    def test_a_manifest_refuses_an_error_page_in_place_of_each_model(self):
        for magic in ("GGML", "GGUF", "ONNX", "ZIP", "JSON"):
            with self.subTest(magic=magic):
                result = self.run_manifest(
                    f"model.bin|https://example.invalid/model.bin|{magic}",
                    {"model.bin": HTML},
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(f"not a {magic} file", result.stderr)
                self.assertFalse((self.models / "model.bin").exists())
                self.assertFalse((self.models / "model.bin.part").exists())

    def test_a_manifest_entry_may_name_a_file_inside_a_directory(self):
        # The content model is three files in one directory, and the operator should
        # not have to create that directory before the fetcher can fill it.
        result = self.run_manifest(
            "hubert/config.json|https://example.invalid/cfg|JSON\n",
            {"cfg": b'{"hidden_size": 768}'},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b'{"hidden_size": 768}', (self.models / "hubert" / "config.json").read_bytes())

    def test_an_empty_manifest_says_so_instead_of_doing_nothing(self):
        result = self.run_manifest("", {})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("VOICE_MANIFEST", result.stderr)

    def test_a_magic_can_be_declared_unverified(self):
        result = self.run_manifest(
            "model.bin|https://example.invalid/model.bin|none",
            {"model.bin": b"whatever"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.models / "model.bin").exists())

    def test_a_manifest_reports_each_model_it_fetched(self):
        result = self.run_manifest(
            "base.bin|https://example.invalid/base.bin|GGML\n",
            {"base.bin": GGML},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        # The summary line names the file and its size; a silent fetch leaves an
        # operator believing a model is in place when nothing was ever written.
        self.assertIn("model ready: base.bin", result.stdout)

    def test_a_failing_entry_fails_the_whole_manifest(self):
        # A loop fed by a pipe runs in a subshell, so its failure used to be swallowed
        # and the script reported success with models missing.
        result = self.run_manifest(
            "good.bin|https://example.invalid/good.bin|GGML\nbad.bin|https://example.invalid/bad.bin|GGML\n",
            {"good.bin": GGML, "bad.bin": HTML},
        )
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.models / "bad.bin").exists())
        self.assertTrue((self.models / "good.bin").exists())

    def test_an_existing_model_is_left_alone(self):
        (self.models / "model.gguf").write_bytes(GGUF)
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("already present", result.stdout)


if __name__ == "__main__":
    unittest.main()
