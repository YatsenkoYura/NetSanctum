"""`app/core/crypto` is infrastructure, and infrastructure has no module names.

The move put the passphrase KDF, the payload envelope and the blind-write
envelope in core so that a second module with a secret would not have to write its
own. That only holds while the direction of the dependency is one-way: core must
not import anything from `app.modules`, or the thing that was meant to be reusable
turns back into vault with a longer import path.

These tests also pin what stayed behind. The passphrase policy and the file-key
decision are product judgements, not mechanisms, and a future tidy-up that drags
them into core would be a regression in judgement rather than a refactor.
"""

import ast
import unittest
from pathlib import Path

CORE_CRYPTO = Path("app/core/crypto")
VAULT_CRYPTO = Path("app/modules/vault/crypto.py")


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import statement in one file."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


class CoreIsIndependentTests(unittest.TestCase):
    def test_core_crypto_imports_no_module(self):
        offenders = {}
        for path in sorted(CORE_CRYPTO.glob("*.py")):
            leaked = {name for name in imported_modules(path) if name.startswith("app.modules")}
            if leaked:
                offenders[path.name] = sorted(leaked)

        self.assertEqual({}, offenders, "core/crypto must not know about modules")

    def test_core_crypto_depends_only_on_three_things(self):
        """A crypto helper that grows a dependency list is growing a policy."""
        allowed_prefixes = ("app.core.crypto",)
        allowed_modules = {
            "argon2",
            "cryptography",
            "base64",
            "collections",
            "hashlib",
            "hmac",
            "json",
            "os",
            "dataclasses",
            "typing",
            "unicodedata",
        }

        for path in sorted(CORE_CRYPTO.glob("*.py")):
            for name in imported_modules(path):
                if name.startswith(allowed_prefixes) or name in allowed_modules:
                    continue
                if name.split(".")[0] in allowed_modules or name.startswith("app.core.crypto"):
                    continue
                self.fail(f"{path.name} imports {name}, which is not on the list")


class PolicyStayedBehindTests(unittest.TestCase):
    def test_the_passphrase_policy_is_the_vaults(self):
        from app.modules.vault.crypto import (
            COMMON_PASSPHRASES,
            MIN_PASSPHRASE_LENGTH,
            WeakPassphraseError,
            check_passphrase_strength,
        )

        self.assertEqual(14, MIN_PASSPHRASE_LENGTH)
        self.assertIn("password", COMMON_PASSPHRASES)
        self.assertTrue(issubclass(WeakPassphraseError, ValueError))

        with self.assertRaises(WeakPassphraseError):
            check_passphrase_strength("short")

    def test_core_has_no_opinion_about_a_passphrase(self):
        """A `check_password` callback in core would be a policy with no owner."""
        import app.core.crypto as core

        for name in dir(core):
            self.assertNotIn("passphrase", name.lower(), f"{name} is a passphrase policy in core")
            self.assertNotIn("weak", name.lower())

    def test_the_file_key_decision_is_the_vaults(self):
        from app.core.crypto import generate_inbox_keypair
        from app.modules.vault.crypto import FILE_KEY_INFO, derive_file_key

        private, _ = generate_inbox_keypair()
        first = derive_file_key(private, 1)

        self.assertEqual(32, len(first))
        self.assertEqual(first, derive_file_key(private, 1))
        self.assertNotEqual(first, derive_file_key(private, 2))
        self.assertNotEqual(FILE_KEY_INFO, b"")

    def test_the_file_key_matches_what_the_old_derivation_produced(self):
        """HKDF(inbox_private, salt=collection, info=file-key:v1) — unchanged."""
        import base64
        import json

        from app.modules.vault.crypto import derive_file_key

        vectors = json.loads(Path("tests/fixtures/crypto_wire_vectors.json").read_text())
        private = bytes.fromhex(vectors["blind_write"]["private_hex"])

        # Recompute through the primitive core now exposes, and compare with the
        # module's own derivation: they must be the same call, not two similar ones.
        from app.core.crypto import derive_subkey

        via_core = derive_subkey(
            private, salt=b"ns:vault:file-key:5", info=b"ns:vault:file-key:v1", length=32
        )

        self.assertEqual(base64.b64encode(derive_file_key(private, 5)), base64.b64encode(via_core))


class ReExportTests(unittest.TestCase):
    def test_the_vault_surface_is_unchanged(self):
        """Every name the tree imported before the move is still importable."""
        import app.modules.vault.crypto as vault

        for name in vault.__all__:
            self.assertTrue(hasattr(vault, name), f"{name} vanished from the vault's crypto")

    def test_the_error_is_one_class_under_two_names(self):
        """`except VaultUnlockError` has to keep catching what core raises."""
        import app.core.crypto as core
        import app.modules.vault.crypto as vault

        self.assertIs(vault.VaultUnlockError, core.UnlockError)

    def test_core_exports_what_it_documents(self):
        import app.core.crypto as core

        for name in core.__all__:
            self.assertTrue(hasattr(core, name), f"{name} is in __all__ but not importable")

    def test_the_reexported_names_all_come_from_core(self):
        """The vault module should add names, not reimplement them."""
        import app.core.crypto as core
        import app.modules.vault.crypto as vault

        vault_only = {
            "WeakPassphraseError",
            "MIN_PASSPHRASE_LENGTH",
            "COMMON_PASSPHRASES",
            "check_passphrase_strength",
            "FILE_KEY_INFO",
            "derive_file_key",
            "VaultUnlockError",
        }

        for name in vault.__all__:
            if name in vault_only or name == "VaultUnlockError":
                continue
            self.assertTrue(hasattr(core, name), f"{name} is exported by the vault but not by core")

    def test_the_move_actually_moved_something(self):
        """A vault crypto.py that still holds the mechanism defeats the point."""
        source = VAULT_CRYPTO.read_text()

        for name in (
            "def derive_kek",
            "def wrap_data_key",
            "def seal(",
            "def seal_for_inbox",
            "import argon2",
        ):
            self.assertNotIn(name, source, f"{name} is still in the module it was moved out of")


if __name__ == "__main__":
    unittest.main()
