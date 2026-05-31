"""
Tests for KnowledgeBase HMAC manifest integrity and async FAISS dispatch.
"""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vmware_ai_ops_agent.analysis.knowledge_base import (
    HMAC_SIGNATURE_FILE,
    MANIFEST_FILE,
    KnowledgeBase,
)
from vmware_ai_ops_agent.config import KnowledgeBaseConfig, VectorDBConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kb(tmp_path: Path, signing_secret: str = "test-signing-secret") -> KnowledgeBase:
    vector_cfg = VectorDBConfig(persist_directory=str(tmp_path))
    kb_cfg = KnowledgeBaseConfig()
    return KnowledgeBase(vector_cfg, kb_cfg, api_key="test-api-key", signing_secret=signing_secret)


def _write_fake_index(tmp_path: Path, faiss_content: bytes = b"FAISS", pkl_content: bytes = b"PKL"):
    (tmp_path / "index.faiss").write_bytes(faiss_content)
    (tmp_path / "index.pkl").write_bytes(pkl_content)


def _sign_manifest(tmp_path: Path, signing_secret: str = "test-signing-secret") -> None:
    """Write a valid manifest + HMAC for the fake files."""
    key = hashlib.sha256(signing_secret.encode()).digest()
    manifest: dict[str, str] = {}
    for name in ("index.faiss", "index.pkl"):
        p = tmp_path / name
        if p.exists():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            manifest[name] = sha
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    h = hmac.new(key, manifest_bytes, digestmod=hashlib.sha256)
    (tmp_path / MANIFEST_FILE).write_bytes(manifest_bytes)
    (tmp_path / HMAC_SIGNATURE_FILE).write_text(h.hexdigest())


# ---------------------------------------------------------------------------
# Manifest signing tests
# ---------------------------------------------------------------------------


class TestKnowledgeBaseSignature:
    def test_save_and_verify_manifest(self, tmp_path):
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)

        kb._save_signature(tmp_path)

        assert (tmp_path / MANIFEST_FILE).exists()
        assert (tmp_path / HMAC_SIGNATURE_FILE).exists()
        assert kb._verify_signature(tmp_path) is True

    def test_tampered_faiss_detected(self, tmp_path):
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)
        kb._save_signature(tmp_path)

        (tmp_path / "index.faiss").write_bytes(b"TAMPERED")
        assert kb._verify_signature(tmp_path) is False

    def test_tampered_pkl_detected(self, tmp_path):
        """Tampering index.pkl must be caught — this was the original S1 gap."""
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)
        kb._save_signature(tmp_path)

        (tmp_path / "index.pkl").write_bytes(b"MALICIOUS_PICKLE")
        assert kb._verify_signature(tmp_path) is False

    def test_missing_manifest_falls_back_gracefully(self, tmp_path):
        """No signature files → allow with warning (first-time load)."""
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)
        # No signature written yet
        assert kb._verify_signature(tmp_path) is True

    def test_wrong_signing_secret_fails(self, tmp_path):
        _write_fake_index(tmp_path)
        # Sign with one secret
        kb_writer = _make_kb(tmp_path, signing_secret="correct-secret")
        kb_writer._save_signature(tmp_path)

        # Verify with a different secret
        kb_reader = _make_kb(tmp_path, signing_secret="wrong-secret")
        assert kb_reader._verify_signature(tmp_path) is False

    def test_manifest_covers_both_files(self, tmp_path):
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)
        kb._save_signature(tmp_path)

        manifest = json.loads((tmp_path / MANIFEST_FILE).read_bytes())
        assert "index.faiss" in manifest
        assert "index.pkl" in manifest

    def test_hmac_key_uses_signing_secret_over_api_key(self, tmp_path):
        kb = _make_kb(tmp_path, signing_secret="my-secret")
        key = kb._hmac_key()
        assert key == hashlib.sha256(b"my-secret").digest()

    def test_hmac_key_falls_back_to_api_key_when_no_secret(self, tmp_path):
        kb = _make_kb(tmp_path, signing_secret="")
        key = kb._hmac_key()
        assert key == hashlib.sha256(b"test-api-key").digest()

    def test_legacy_single_file_hmac_accepted(self, tmp_path):
        """Old indexes with only index.hmac (no manifest) still load with a warning."""
        _write_fake_index(tmp_path)
        kb = _make_kb(tmp_path)
        # Write legacy single-file HMAC (index.faiss only)
        index_file = tmp_path / "index.faiss"
        h = hmac.new(kb._hmac_key(), digestmod=hashlib.sha256)
        with open(index_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        (tmp_path / HMAC_SIGNATURE_FILE).write_text(h.hexdigest())
        # No MANIFEST_FILE written
        assert kb._verify_signature(tmp_path) is True


# ---------------------------------------------------------------------------
# Async FAISS dispatch (P1)
# ---------------------------------------------------------------------------


class TestKnowledgeBaseAsync:
    @pytest.mark.asyncio
    async def test_search_similar_uses_thread(self, tmp_path):
        """search_similar must not call similarity_search_with_score on the event loop."""
        kb = _make_kb(tmp_path)
        kb._initialized = True

        mock_db = MagicMock()
        mock_db.similarity_search_with_score.return_value = []
        kb._db = mock_db

        calls_from_thread = []

        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(func, *args, **kwargs):
            calls_from_thread.append(func)
            return await original_to_thread(func, *args, **kwargs)

        with patch(
            "vmware_ai_ops_agent.analysis.knowledge_base.asyncio.to_thread",
            side_effect=tracking_to_thread,
        ):
            await kb.search_similar("test query")

        assert len(calls_from_thread) >= 1, "search_similar should dispatch to thread"

    @pytest.mark.asyncio
    async def test_initialize_loads_index_in_thread(self, tmp_path):
        """initialize() must load FAISS from a thread, not the event loop."""
        _write_fake_index(tmp_path)
        _sign_manifest(tmp_path)

        kb = _make_kb(tmp_path)
        kb._embeddings = MagicMock()

        thread_fns = []

        async def tracking_to_thread(func, *args, **kwargs):
            thread_fns.append(True)
            # Return a mock FAISS db
            mock_db = MagicMock()
            mock_db.index = MagicMock()
            mock_db.index.ntotal = 0
            return mock_db

        with (
            patch(
                "vmware_ai_ops_agent.analysis.knowledge_base.asyncio.to_thread",
                side_effect=tracking_to_thread,
            ),
            patch(
                "vmware_ai_ops_agent.analysis.knowledge_base.OpenAIEmbeddings",
                return_value=kb._embeddings,
            ),
        ):
            await kb.initialize()

        # to_thread should have been called at least for verify_signature + load_local
        assert len(thread_fns) >= 2
