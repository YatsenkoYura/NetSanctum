"""A vault sealed under scrypt must survive the move to Argon2id.

The wrapper is the only thing standing between a stolen database and every card in
a sealed collection, and it stores the cost it was sealed with. Two things follow
from the switch, and both are easy to get wrong in a way no unit test notices
until somebody cannot open their own vault:

* reading it back has to use the stored parameters, not the current defaults, or
  every existing vault reports a wrong passphrase;
* a vault created before the switch should end up on the new one, otherwise the
  security change only reaches vaults that do not exist yet.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.modules.vault.crypto import (
    KDF_NAME,
    LEGACY_KDF_NAME,
    VaultUnlockError,
    context_for,
    new_data_key,
    unwrap_data_key,
    wrap_data_key,
)
from app.modules.vault.models import VaultCollection
from app.modules.vault.sealing import store_wrapper, unwrap_data_key as unwrap_stored, upgrade_wrapper

SCRYPT = {"kdf": LEGACY_KDF_NAME, "n": 2**8, "r": 8, "p": 1}
ARGON = {"t_cost": 1, "m_cost": 8, "parallelism": 1}


class AsyncSessionAdapter:
    """The slice of AsyncSession the sealing code touches."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, instance):
        self.session.refresh(instance)


class WrapperRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)
        self.session = AsyncSessionAdapter(self.db)

    def collection(self, collection_id=1):
        row = VaultCollection(id=collection_id, name="Приватное", is_encrypted=True)
        self.db.add(row)
        self.db.commit()
        return row

    def test_argon2_parameters_survive_the_database(self):
        from app.modules.vault.sealing import wrapper_for

        row = self.collection()
        store_wrapper(
            row, wrap_data_key(new_data_key(), "pass", context=context_for("collection", 1), **ARGON)
        )
        self.db.commit()
        self.db.expire_all()

        restored = wrapper_for(self.db.get(VaultCollection, 1))
        self.assertEqual(KDF_NAME, restored.kdf)
        self.assertEqual(1, restored.t_cost)
        self.assertEqual(8, restored.m_cost)
        self.assertEqual(1, restored.parallelism)

    def test_a_legacy_row_opens_with_the_cost_it_was_sealed_at(self):
        """A vault sealed before the switch stores scrypt and scrypt's cost.

        Reading it with the current defaults would derive a different key and tell
        their owner their passphrase is wrong — the most confusing possible answer,
        because it is not wrong.
        """
        from app.modules.vault.sealing import wrapper_for

        row = self.collection()
        secret = new_data_key()
        store_wrapper(row, wrap_data_key(secret, "pass", context=context_for("collection", 1), **SCRYPT))
        self.db.commit()
        self.db.expire_all()

        restored = wrapper_for(self.db.get(VaultCollection, 1))
        self.assertEqual(LEGACY_KDF_NAME, restored.kdf)
        self.assertEqual(secret, unwrap_data_key(restored, "pass", context=context_for("collection", 1)))

    def test_a_row_with_no_derivation_cost_is_refused_by_name(self):
        """It cannot be derived by guessing: the cost is part of the key.

        Both columns arrived in one migration and are always written together, so
        this state means a damaged row. Reporting a wrong passphrase would send the
        owner looking for a typo that is not there.
        """
        from app.modules.vault.crypto import VaultUnlockError
        from app.modules.vault.sealing import wrapper_for

        row = self.collection()
        store_wrapper(
            row, wrap_data_key(new_data_key(), "pass", context=context_for("collection", 1), **SCRYPT)
        )
        row.key_kdf = None
        row.key_kdf_params = None
        self.db.commit()

        with self.assertRaises(VaultUnlockError) as caught:
            wrapper_for(self.db.get(VaultCollection, 1))
        self.assertIn("derivation cost", str(caught.exception))
        self.assertNotIn("passphrase", str(caught.exception))

    def test_the_key_survives_being_moved_to_argon2id(self):
        """What the upgrade actually promises: the same key, a harder wrapper."""
        from app.modules.vault.sealing import wrapper_for

        row = self.collection()
        secret = new_data_key()
        store_wrapper(row, wrap_data_key(secret, "pass", context=context_for("collection", 1), **SCRYPT))
        self.db.commit()

        asyncio.run(upgrade_wrapper(self.session, row, secret, "pass"))
        self.db.expire_all()

        restored = wrapper_for(self.db.get(VaultCollection, 1))
        self.assertEqual(KDF_NAME, restored.kdf)
        self.assertEqual(secret, unwrap_data_key(restored, "pass", context=context_for("collection", 1)))

    def test_a_vault_already_on_argon2id_is_left_alone(self):
        row = self.collection()
        store_wrapper(
            row, wrap_data_key(new_data_key(), "pass", context=context_for("collection", 1), **ARGON)
        )
        self.db.commit()
        before = (row.key_kdf, row.wrapped_key)

        self.assertFalse(asyncio.run(upgrade_wrapper(self.session, row, new_data_key(), "pass")))
        self.assertEqual(before, (row.key_kdf, row.wrapped_key))

    def test_a_failed_upgrade_keeps_the_old_wrapper_and_says_so(self):
        """The owner has already proved the passphrase; a write failure must not
        turn that into a failed unlock."""
        row = self.collection()
        secret = new_data_key()
        store_wrapper(row, wrap_data_key(secret, "pass", context=context_for("collection", 1), **SCRYPT))
        self.db.commit()

        session = AsyncSessionAdapter(self.db)
        session.commit = AsyncMock(side_effect=RuntimeError("disk full"))

        with self.assertLogs("app.modules.vault.sealing", level="ERROR"):
            self.assertFalse(asyncio.run(upgrade_wrapper(session, row, secret, "pass")))
        self.assertEqual(LEGACY_KDF_NAME, row.key_kdf)

    def test_the_wrong_passphrase_is_still_refused_before_anything_is_written(self):
        from app.modules.vault.sealing import wrapper_for

        row = self.collection()
        store_wrapper(
            row, wrap_data_key(new_data_key(), "pass", context=context_for("collection", 1), **SCRYPT)
        )
        self.db.commit()
        before = row.wrapped_key

        with self.assertRaises(VaultUnlockError):
            unwrap_data_key(wrapper_for(row), "not it", context=context_for("collection", 1))
        self.assertEqual(before, row.wrapped_key)


class UnlockUpgradeTests(unittest.TestCase):
    """`unlock_collection` is where the passphrase is proved, so it upgrades."""

    def test_unlocking_a_legacy_vault_rewrites_its_wrapper(self):
        from app.modules.vault.models import VaultCollection
        from app.modules.vault.sealing import unlock_collection, wrapper_for

        secret = new_data_key()
        row = VaultCollection(id=5, name="Приватное", is_encrypted=True)
        store_wrapper(row, wrap_data_key(secret, "pass", context=context_for("collection", 5), **SCRYPT))

        with patch("app.modules.vault.sealing.redis_client") as redis:
            redis.set = AsyncMock()
            asyncio.run(unlock_collection(row, "pass", "tok", session=AsyncMock()))
            redis.set.assert_awaited()

        self.assertEqual(KDF_NAME, row.key_kdf)
        self.assertEqual(
            secret, unwrap_stored(wrapper_for(row), "pass", context=context_for("collection", 5))
        )

    def test_unlocking_without_a_session_changes_nothing(self):
        """The upgrade needs somewhere to write; without a session it is skipped
        rather than half-applied."""
        from app.modules.vault.models import VaultCollection
        from app.modules.vault.sealing import unlock_collection

        row = VaultCollection(id=6, name="Приватное", is_encrypted=True)
        store_wrapper(
            row, wrap_data_key(new_data_key(), "pass", context=context_for("collection", 6), **SCRYPT)
        )

        with patch("app.modules.vault.sealing.redis_client") as redis:
            redis.set = AsyncMock()
            asyncio.run(unlock_collection(row, "pass", "tok"))

        self.assertEqual(LEGACY_KDF_NAME, row.key_kdf)
