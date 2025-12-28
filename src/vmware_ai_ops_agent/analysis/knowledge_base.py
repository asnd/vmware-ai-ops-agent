"""
Knowledge base for storing and retrieving similar incidents.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

try:
    import chromadb
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False

from ..config import KnowledgeBaseConfig, VectorDBConfig
from .models import AnalysisResult

logger = structlog.get_logger(__name__)


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
    """Knowledge base for VMware infrastructure operations."""

    def __init__(self, vector_config: VectorDBConfig, kb_config: KnowledgeBaseConfig):
        self.vector_config = vector_config
        self.kb_config = kb_config
        self._client: Any = None
        self._collection: Any = None
        self._initialized = False

    async def initialize(self) -> None:
        if not CHROMADB_AVAILABLE:
            logger.warning("ChromaDB not available, knowledge base disabled")
            return

        try:
            persist_dir = Path(self.vector_config.persist_directory)
            persist_dir.mkdir(parents=True, exist_ok=True)

            self._client = chromadb.PersistentClient(path=str(persist_dir))
            self._collection = self._client.get_or_create_collection(
                name=self.vector_config.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            self._initialized = True
            logger.info("Knowledge base initialized", documents=self._collection.count())
        except Exception as e:
            logger.error("Failed to initialize knowledge base", error=str(e))
            self._initialized = False

    async def add_incident(self, incident: Incident) -> None:
        if not self._initialized:
            return

        document = f"Incident: {incident.summary}\nRoot Cause: {incident.root_cause}\nResolution: {incident.resolution}"

        try:
            self._collection.add(
                ids=[incident.id],
                documents=[document],
                metadatas=[{"type": "incident", "summary": incident.summary}],
            )
            logger.debug("Added incident to knowledge base", id=incident.id)
        except Exception as e:
            logger.error("Failed to add incident", error=str(e))

    async def search_similar(self, query: str, n_results: int = 5) -> list[SimilarityResult]:
        if not self._initialized:
            return []

        try:
            results = self._collection.query(query_texts=[query], n_results=n_results)

            similar = []
            for i, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results.get("distances") else 0
                similar.append(
                    SimilarityResult(
                        id=doc_id,
                        document_type=results["metadatas"][0][i].get("type", "unknown"),
                        content=results["documents"][0][i],
                        similarity_score=1 - distance,
                        metadata=results["metadatas"][0][i],
                    )
                )
            return similar
        except Exception as e:
            logger.error("Similarity search failed", error=str(e))
            return []

    async def record_analysis(self, analysis: AnalysisResult, resolution: str | None = None) -> None:
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
        if not self._initialized:
            return {"initialized": False, "documents": 0}
        return {
            "initialized": True,
            "documents": self._collection.count(),
            "collection": self.vector_config.collection_name,
        }
