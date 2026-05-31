"""
Knowledge base for storing and retrieving similar incidents.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel, Field

from ..config import KnowledgeBaseConfig, VectorDBConfig
from .models import AnalysisResult

logger = structlog.get_logger(__name__)

# Files written by FAISS.save_local — both must be covered by the manifest.
_FAISS_FILES = ("index.faiss", "index.pkl")

HMAC_SIGNATURE_FILE = "index.hmac"
MANIFEST_FILE = "index.manifest"
# Legacy checksum file kept for read-only backward-compatibility detection.
CHECKSUM_FILE = "index.checksum"


class Incident(BaseModel):
    """Historical incident record."""

    id: str
    timestamp: datetime
    summary: str
    root_cause: str
    resolution: str
    affected_resources: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class SimilarityResult(BaseModel):
    """Result from similarity search."""

    id: str
    document_type: str
    content: str
    similarity_score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBase:
    """Knowledge base for VMware infrastructure operations using FAISS.

    Index integrity is protected with HMAC-SHA256 over a manifest that covers
    *both* FAISS files (index.faiss and index.pkl).  The HMAC key is derived
    from a dedicated signing_secret; falling back to the LLM api_key only when
    no signing_secret is configured (with a deprecation warning).
    """

    def __init__(
        self,
        vector_config: VectorDBConfig,
        kb_config: KnowledgeBaseConfig,
        api_key: str,
        signing_secret: str = "",
    ):
        self.vector_config = vector_config
        self.kb_config = kb_config
        self.api_key = api_key
        self._signing_secret = signing_secret
        self._db: FAISS | None = None
        self._initialized = False
        self._embeddings = None
        self._pending_docs: list[Document] = []
        self._batch_size = 10
        self._dirty = False

    # ------------------------------------------------------------------
    # HMAC helpers
    # ------------------------------------------------------------------

    def _hmac_key(self) -> bytes:
        if self._signing_secret:
            return hashlib.sha256(self._signing_secret.encode()).digest()
        logger.warning(
            "knowledge_base.signing_secret not set; falling back to LLM api_key for HMAC. "
            "Set VMWARE_AI__KNOWLEDGE_BASE__SIGNING_SECRET to a dedicated secret."
        )
        return hashlib.sha256(self.api_key.encode()).digest()

    def _file_sha256(self, path: Path) -> str:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def _build_manifest(self, persist_dir: Path) -> dict[str, str]:
        """Return {filename: sha256hex} for every FAISS file that exists."""
        manifest: dict[str, str] = {}
        for name in _FAISS_FILES:
            p = persist_dir / name
            if p.exists():
                manifest[name] = self._file_sha256(p)
        return manifest

    def _save_signature(self, persist_dir: Path) -> None:
        manifest = self._build_manifest(persist_dir)
        if not manifest:
            return
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        h = hmac.new(self._hmac_key(), manifest_bytes, digestmod=hashlib.sha256)
        (persist_dir / MANIFEST_FILE).write_bytes(manifest_bytes)
        (persist_dir / HMAC_SIGNATURE_FILE).write_text(h.hexdigest())

    def _verify_signature(self, persist_dir: Path) -> bool:
        """Verify index integrity.

        Priority:
        1. Manifest HMAC (covers both index.faiss and index.pkl).
        2. Legacy single-file HMAC (index.faiss only) — warns and accepts.
        3. No signature files — warns and accepts (first-time loads).
        """
        hmac_file = persist_dir / HMAC_SIGNATURE_FILE
        manifest_file = persist_dir / MANIFEST_FILE

        if hmac_file.exists() and manifest_file.exists():
            return self._verify_manifest_hmac(persist_dir, hmac_file, manifest_file)

        if hmac_file.exists():
            # Legacy: HMAC was written before manifest support, covers index.faiss only.
            logger.warning(
                "FAISS index has legacy single-file HMAC (index.pkl not covered). "
                "Re-save the index to upgrade to manifest-based signing."
            )
            return self._verify_legacy_file_hmac(persist_dir, hmac_file)

        logger.warning("No HMAC signature found for FAISS index — skipping integrity check")
        return True

    def _verify_manifest_hmac(
        self, persist_dir: Path, hmac_file: Path, manifest_file: Path
    ) -> bool:
        stored = hmac_file.read_text().strip()
        manifest_bytes = manifest_file.read_bytes()
        h = hmac.new(self._hmac_key(), manifest_bytes, digestmod=hashlib.sha256)
        if not hmac.compare_digest(stored, h.hexdigest()):
            logger.error("FAISS index manifest HMAC verification failed — possible tampering")
            return False

        # Verify the on-disk files still match the manifest hashes.
        manifest = json.loads(manifest_bytes)
        for name, expected_sha in manifest.items():
            p = persist_dir / name
            if not p.exists():
                logger.error("FAISS file listed in manifest is missing", file=name)
                return False
            if self._file_sha256(p) != expected_sha:
                logger.error("FAISS file content mismatch vs manifest", file=name)
                return False

        logger.info("FAISS index manifest HMAC verification passed")
        return True

    def _verify_legacy_file_hmac(self, persist_dir: Path, hmac_file: Path) -> bool:
        stored = hmac_file.read_text().strip()
        index_file = persist_dir / "index.faiss"
        if not index_file.exists():
            return True
        h = hmac.new(self._hmac_key(), digestmod=hashlib.sha256)
        with open(index_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if not hmac.compare_digest(stored, h.hexdigest()):
            logger.error("FAISS index legacy HMAC verification failed")
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        try:
            if not self.api_key:
                logger.warning("No API key provided, knowledge base disabled")
                return

            self._embeddings = OpenAIEmbeddings(api_key=self.api_key)

            persist_dir = Path(self.vector_config.persist_directory)
            index_file = persist_dir / "index.faiss"

            if index_file.exists():
                ok = await asyncio.to_thread(self._verify_signature, persist_dir)
                if not ok:
                    logger.error("FAISS index failed integrity verification, starting fresh")
                    self._db = None
                    self._initialized = True
                    return

                logger.warning(
                    "Loading FAISS index with allow_dangerous_deserialization=True. "
                    "Integrity verified via HMAC-SHA256 manifest."
                )
                logger.info("Loading existing FAISS index", path=str(persist_dir))
                embeddings = self._embeddings
                self._db = await asyncio.to_thread(
                    lambda: FAISS.load_local(
                        str(persist_dir),
                        embeddings,
                        allow_dangerous_deserialization=True,
                    )
                )
            else:
                logger.info("Creating new FAISS index (lazy — first document triggers init)")
                self._db = None

            self._initialized = True
            logger.info("Knowledge base initialized")
        except Exception as e:
            logger.error("Failed to initialize knowledge base", error=str(e))
            self._initialized = False

    async def add_incident(self, incident: Incident) -> None:
        if not self._initialized:
            return

        document_content = (
            f"Incident: {incident.summary}\n"
            f"Root Cause: {incident.root_cause}\n"
            f"Resolution: {incident.resolution}"
        )
        metadata = {
            "id": incident.id,
            "type": "incident",
            "summary": incident.summary,
            "timestamp": incident.timestamp.isoformat(),
            "root_cause": incident.root_cause,
        }

        try:
            doc = Document(page_content=document_content, metadata=metadata)
            self._pending_docs.append(doc)
            self._dirty = True

            if len(self._pending_docs) >= self._batch_size:
                await self._flush_pending()

            logger.debug(
                "Added incident to knowledge base", id=incident.id, pending=len(self._pending_docs)
            )
        except Exception as e:
            logger.error("Failed to add incident", error=str(e))

    async def _flush_pending(self) -> None:
        if not self._pending_docs:
            return

        if not self._initialized or self._embeddings is None:
            logger.error("Cannot flush: knowledge base not properly initialized")
            return

        try:
            docs = self._pending_docs[:]
            embeddings = self._embeddings

            if self._db is None:
                self._db = await asyncio.to_thread(lambda: FAISS.from_documents(docs, embeddings))
            else:
                db = self._db
                await asyncio.to_thread(lambda: db.add_documents(docs))

            persist_dir = Path(self.vector_config.persist_directory)
            persist_dir.mkdir(parents=True, exist_ok=True)
            db = self._db
            await asyncio.to_thread(lambda: db.save_local(str(persist_dir)))
            await asyncio.to_thread(self._save_signature, persist_dir)

            logger.info("Flushed knowledge base", documents=len(self._pending_docs))
            self._pending_docs = []
            self._dirty = False
        except Exception as e:
            logger.error("Failed to flush knowledge base", error=str(e))

    async def flush(self) -> None:
        """Force flush pending documents (e.g., on shutdown)."""
        if self._dirty:
            await self._flush_pending()

    async def search_similar(self, query: str, n_results: int = 5) -> list[SimilarityResult]:
        if not self._initialized or self._db is None:
            return []

        try:
            db = self._db
            results = await asyncio.to_thread(
                lambda: db.similarity_search_with_score(query, k=n_results)
            )

            similar = []
            for doc, score in results:
                similarity_score = 1.0 / (1.0 + score)
                similar.append(
                    SimilarityResult(
                        id=doc.metadata.get("id", "unknown"),
                        document_type=doc.metadata.get("type", "unknown"),
                        content=doc.page_content,
                        similarity_score=similarity_score,
                        metadata=doc.metadata,
                    )
                )
            return similar
        except Exception as e:
            logger.error("Similarity search failed", error=str(e))
            return []

    async def record_analysis(
        self, analysis: AnalysisResult, resolution: str | None = None
    ) -> None:
        if not self._initialized:
            return

        incident = Incident(
            id=f"incident-{analysis.id}",
            timestamp=analysis.analyzed_at,
            summary=analysis.summary,
            root_cause=analysis.root_cause.primary_cause if analysis.root_cause else "Unknown",
            resolution=resolution or "Pending",
            symptoms=analysis.insights,
            tags=[analysis.urgency.value],
        )
        await self.add_incident(incident)

    def get_statistics(self) -> dict[str, Any]:
        if not self._initialized or self._db is None:
            return {
                "initialized": self._initialized,
                "documents": 0,
                "pending": len(self._pending_docs),
            }
        return {
            "initialized": True,
            "documents": self._db.index.ntotal,
            "pending": len(self._pending_docs),
            "collection": self.vector_config.collection_name,
        }
