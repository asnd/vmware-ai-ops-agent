"""
Knowledge base for storing and retrieving similar incidents.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from langchain_core.documents import Document

from ..config import KnowledgeBaseConfig, VectorDBConfig
from .models import AnalysisResult

logger = structlog.get_logger(__name__)

# Checksum file for FAISS index integrity validation
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
    """Knowledge base for VMware infrastructure operations using FAISS."""

    def __init__(self, vector_config: VectorDBConfig, kb_config: KnowledgeBaseConfig, api_key: str):
        self.vector_config = vector_config
        self.kb_config = kb_config
        self.api_key = api_key
        self._db: FAISS | None = None
        self._initialized = False
        self._embeddings = OpenAIEmbeddings(api_key=api_key)
        # Batch save settings
        self._pending_docs: list[Document] = []
        self._batch_size = 10  # Save after N documents
        self._dirty = False  # Track if there are unsaved changes

    def _compute_checksum(self, persist_dir: Path) -> str:
        """Compute SHA256 checksum of FAISS index file."""
        index_file = persist_dir / "index.faiss"
        if not index_file.exists():
            return ""
        sha256 = hashlib.sha256()
        with open(index_file, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _save_checksum(self, persist_dir: Path) -> None:
        """Save checksum to file."""
        checksum = self._compute_checksum(persist_dir)
        checksum_file = persist_dir / CHECKSUM_FILE
        checksum_file.write_text(checksum)

    def _verify_checksum(self, persist_dir: Path) -> bool:
        """Verify FAISS index integrity before loading."""
        checksum_file = persist_dir / CHECKSUM_FILE
        if not checksum_file.exists():
            logger.warning("No checksum file found, skipping integrity check")
            return True  # Allow first-time loads without checksum
        stored_checksum = checksum_file.read_text().strip()
        computed_checksum = self._compute_checksum(persist_dir)
        if stored_checksum != computed_checksum:
            logger.error("FAISS index integrity check failed",
                        stored=stored_checksum[:16], computed=computed_checksum[:16])
            return False
        return True

    async def initialize(self) -> None:
        try:
            persist_dir = Path(self.vector_config.persist_directory)
            index_file = persist_dir / "index.faiss"

            if index_file.exists():
                # Security: Verify index integrity before loading
                if not self._verify_checksum(persist_dir):
                    logger.error("FAISS index failed integrity check, starting fresh")
                    self._db = None
                    self._initialized = True
                    return

                logger.info("Loading existing FAISS index", path=str(persist_dir))
                self._db = FAISS.load_local(str(persist_dir), self._embeddings, allow_dangerous_deserialization=True)
            else:
                logger.info("Creating new FAISS index")
                # FAISS requires at least one document to initialize if not loading from disk
                # We will handle lazy init in add_incident.
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
            "root_cause": incident.root_cause
        }

        try:
            doc = Document(page_content=document_content, metadata=metadata)
            self._pending_docs.append(doc)
            self._dirty = True

            # Batch save: only persist when batch size reached
            if len(self._pending_docs) >= self._batch_size:
                await self._flush_pending()

            logger.debug("Added incident to knowledge base", id=incident.id, pending=len(self._pending_docs))
        except Exception as e:
            logger.error("Failed to add incident", error=str(e))

    async def _flush_pending(self) -> None:
        """Flush pending documents to FAISS index and persist to disk."""
        if not self._pending_docs:
            return

        try:
            if self._db is None:
                self._db = FAISS.from_documents(self._pending_docs, self._embeddings)
            else:
                self._db.add_documents(self._pending_docs)

            # Persist to disk with checksum
            persist_dir = Path(self.vector_config.persist_directory)
            persist_dir.mkdir(parents=True, exist_ok=True)
            self._db.save_local(str(persist_dir))
            self._save_checksum(persist_dir)

            logger.info("Flushed knowledge base", documents=len(self._pending_docs))
            self._pending_docs = []
            self._dirty = False
        except Exception as e:
            logger.error("Failed to flush knowledge base", error=str(e))

    async def flush(self) -> None:
        """Public method to force flush pending documents (e.g., on shutdown)."""
        if self._dirty:
            await self._flush_pending()

    async def search_similar(self, query: str, n_results: int = 5) -> list[SimilarityResult]:
        if not self._initialized or self._db is None:
            return []

        try:
            # FAISS similarity search returns (Document, score) tuples
            # score is L2 distance (lower is better) or cosine similarity depending on config.
            # OpenAI embeddings are normalized, so dot product is cosine similarity. 
            # FAISS default is L2. 
            # We will use similarity_search_with_score
            results = self._db.similarity_search_with_score(query, k=n_results)

            similar = []
            for doc, score in results:
                # Convert L2 distance to a 0-1 similarity score roughly
                # Or just return the raw score. 
                # For L2: 0 is identical.
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
            return {"initialized": self._initialized, "documents": 0, "pending": len(self._pending_docs)}
        return {
            "initialized": True,
            "documents": self._db.index.ntotal,
            "pending": len(self._pending_docs),
            "collection": self.vector_config.collection_name,
        }
