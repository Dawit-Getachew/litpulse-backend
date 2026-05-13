"""
RAG Service for LitScholar Full-Text Analysis

Provides PDF text extraction, chunking, embeddings generation,
and retrieval-augmented generation for article analysis.

Uses Emergent LLM Key for OpenAI embeddings and GPT-5.2.
"""
import os
import io
import hashlib
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
import numpy as np
from pydantic import BaseModel

# PDF extraction
import pdfplumber
from PyPDF2 import PdfReader

# Token counting
import tiktoken

logger = logging.getLogger(__name__)


# ============================================================================
# MODELS
# ============================================================================
class TextChunk(BaseModel):
    """Represents a chunk of text for embedding"""
    chunk_id: str
    content: str
    chunk_index: int
    page_number: int
    token_count: int
    metadata: dict = {}


class RAGDocument(BaseModel):
    """Represents a processed document ready for RAG"""
    document_id: str
    pmid: Optional[str] = None
    filename: str
    chunks: List[TextChunk]
    embeddings: Optional[List[List[float]]] = None
    total_pages: int
    total_chunks: int
    created_at: str


class RAGSearchResult(BaseModel):
    """Result from RAG similarity search"""
    content: str
    similarity_score: float
    page_number: int
    chunk_index: int


# ============================================================================
# PDF TEXT EXTRACTION
# ============================================================================
class PDFExtractor:
    """Extracts text from PDF documents"""
    
    def __init__(self, max_pages: int = 100):
        self.max_pages = max_pages
    
    def extract_from_bytes(self, pdf_bytes: bytes, filename: str = "uploaded.pdf") -> Tuple[str, int]:
        """
        Extract text from PDF bytes.
        
        Args:
            pdf_bytes: Raw PDF file bytes
            filename: Name of the file for logging
            
        Returns:
            Tuple of (extracted_text, page_count)
        """
        try:
            # Try pdfplumber first (better for complex layouts)
            text_content = []
            page_count = 0
            
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                page_count = min(len(pdf.pages), self.max_pages)
                
                for i, page in enumerate(pdf.pages[:self.max_pages]):
                    text = page.extract_text()
                    if text:
                        text_content.append(f"\n--- Page {i+1} ---\n{text}")
            
            full_text = "\n".join(text_content)
            
            # Fallback to PyPDF2 if pdfplumber fails
            if not full_text.strip():
                logger.info(f"pdfplumber failed for {filename}, trying PyPDF2")
                reader = PdfReader(io.BytesIO(pdf_bytes))
                page_count = min(len(reader.pages), self.max_pages)
                text_content = []
                
                for i, page in enumerate(reader.pages[:self.max_pages]):
                    text = page.extract_text()
                    if text:
                        text_content.append(f"\n--- Page {i+1} ---\n{text}")
                
                full_text = "\n".join(text_content)
            
            if not full_text.strip():
                raise ValueError("Could not extract text from PDF")
            
            logger.info(f"Extracted {len(full_text)} chars from {page_count} pages of {filename}")
            return full_text, page_count
            
        except Exception as e:
            logger.error(f"PDF extraction failed for {filename}: {str(e)}")
            raise


# ============================================================================
# TEXT CHUNKING
# ============================================================================
class TextChunker:
    """Chunks text into segments suitable for embedding"""
    
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        max_tokens: int = 8000  # Leave headroom for embedding model limit
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_tokens = max_tokens
        self.encoding = tiktoken.get_encoding("cl100k_base")
    
    def count_tokens(self, text: str) -> int:
        """Count tokens in text"""
        return len(self.encoding.encode(text))
    
    def chunk_text(self, text: str, document_id: str) -> List[TextChunk]:
        """
        Split text into overlapping chunks.
        
        Args:
            text: Full text to chunk
            document_id: ID for the document
            
        Returns:
            List of TextChunk objects
        """
        chunks = []
        
        # Split by pages first if page markers exist
        if "--- Page" in text:
            pages = text.split("--- Page")
            pages = [p for p in pages if p.strip()]
        else:
            # Treat as single page
            pages = [(1, text)]
        
        chunk_index = 0
        
        for page_data in pages:
            # Extract page number if available
            if isinstance(page_data, tuple):
                page_num, page_text = page_data
            else:
                # Parse page number from text
                lines = page_data.strip().split("\n", 1)
                if len(lines) > 0 and "---" in lines[0]:
                    try:
                        page_num = int(lines[0].split()[0])
                        page_text = lines[1] if len(lines) > 1 else ""
                    except:
                        page_num = 1
                        page_text = page_data
                else:
                    page_num = 1
                    page_text = page_data
            
            # Chunk the page text
            page_chunks = self._chunk_page_text(
                page_text, page_num, document_id, chunk_index
            )
            chunks.extend(page_chunks)
            chunk_index += len(page_chunks)
        
        return chunks
    
    def _chunk_page_text(
        self, text: str, page_num: int, document_id: str, start_index: int
    ) -> List[TextChunk]:
        """Chunk a single page's text"""
        chunks = []
        
        # Split into paragraphs first
        paragraphs = text.split("\n\n")
        
        current_chunk = ""
        current_tokens = 0
        chunk_idx = start_index
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            para_tokens = self.count_tokens(para)
            
            # If paragraph alone exceeds max, split it
            if para_tokens > self.max_tokens:
                # Save current chunk if any
                if current_chunk:
                    chunks.append(self._create_chunk(
                        current_chunk, chunk_idx, page_num, document_id, current_tokens
                    ))
                    chunk_idx += 1
                    current_chunk = ""
                    current_tokens = 0
                
                # Split large paragraph by sentences
                sentences = para.replace(". ", ".\n").split("\n")
                for sentence in sentences:
                    sent_tokens = self.count_tokens(sentence)
                    if current_tokens + sent_tokens > self.chunk_size:
                        if current_chunk:
                            chunks.append(self._create_chunk(
                                current_chunk, chunk_idx, page_num, document_id, current_tokens
                            ))
                            chunk_idx += 1
                        current_chunk = sentence
                        current_tokens = sent_tokens
                    else:
                        current_chunk += " " + sentence if current_chunk else sentence
                        current_tokens += sent_tokens
            
            elif current_tokens + para_tokens > self.chunk_size:
                # Save current chunk and start new one
                if current_chunk:
                    chunks.append(self._create_chunk(
                        current_chunk, chunk_idx, page_num, document_id, current_tokens
                    ))
                    chunk_idx += 1
                
                # Keep overlap from previous chunk
                overlap_text = current_chunk[-self.chunk_overlap:] if len(current_chunk) > self.chunk_overlap else ""
                current_chunk = overlap_text + " " + para if overlap_text else para
                current_tokens = self.count_tokens(current_chunk)
            else:
                current_chunk += "\n\n" + para if current_chunk else para
                current_tokens += para_tokens
        
        # Don't forget the last chunk
        if current_chunk:
            chunks.append(self._create_chunk(
                current_chunk, chunk_idx, page_num, document_id, current_tokens
            ))
        
        return chunks
    
    def _create_chunk(
        self, content: str, index: int, page_num: int, doc_id: str, tokens: int
    ) -> TextChunk:
        """Create a TextChunk object"""
        chunk_id = f"{doc_id}_chunk_{index}"
        return TextChunk(
            chunk_id=chunk_id,
            content=content.strip(),
            chunk_index=index,
            page_number=page_num,
            token_count=tokens,
            metadata={"document_id": doc_id}
        )


# ============================================================================
# EMBEDDING SERVICE (using Emergent LLM Key)
# ============================================================================
class EmbeddingService:
    """Generates embeddings using OpenAI via Emergent LLM Key"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize embedding service.
        
        Uses emergentintegrations for OpenAI access with Universal Key.
        """
        self.api_key = api_key
        self._client = None
    
    def _get_client(self):
        """Lazy initialize the OpenAI client"""
        if self._client is None:
            try:
                from emergentintegrations.llm.openai import OpenAIConfig, get_openai_client
                
                config = OpenAIConfig(emergent_api_key=self.api_key)
                self._client = get_openai_client(config)
            except ImportError:
                # Fallback to direct OpenAI client
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key or os.environ.get("OPENAI_API_KEY"))
        return self._client
    
    def embed_texts(self, texts: List[str], batch_size: int = 20) -> List[List[float]]:
        """
        Generate embeddings for a list of texts.
        
        Args:
            texts: List of text strings to embed
            batch_size: Number of texts per API call
            
        Returns:
            List of embedding vectors
        """
        client = self._get_client()
        all_embeddings = []
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            
            try:
                response = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=batch,
                    encoding_format="float"
                )
                
                # Sort by index to preserve order
                batch_embeddings = [None] * len(batch)
                for data in response.data:
                    batch_embeddings[data.index] = data.embedding
                
                all_embeddings.extend(batch_embeddings)
                
            except Exception as e:
                logger.error(f"Embedding batch {i//batch_size} failed: {str(e)}")
                raise
        
        return all_embeddings
    
    def embed_query(self, query: str) -> List[float]:
        """Generate embedding for a single query"""
        embeddings = self.embed_texts([query])
        return embeddings[0]


# ============================================================================
# VECTOR STORE (In-Memory for Session)
# ============================================================================
class InMemoryVectorStore:
    """Simple in-memory vector store for session-based RAG"""
    
    def __init__(self):
        self.documents: Dict[str, RAGDocument] = {}
        self.embeddings: Dict[str, np.ndarray] = {}
        self.chunks: Dict[str, TextChunk] = {}
    
    def add_document(self, document: RAGDocument):
        """Add a document with its embeddings"""
        self.documents[document.document_id] = document
        
        if document.embeddings:
            # Store embeddings as numpy array for efficient search
            self.embeddings[document.document_id] = np.array(document.embeddings)
            
            # Index chunks for retrieval
            for chunk in document.chunks:
                self.chunks[chunk.chunk_id] = chunk
    
    def search(
        self,
        query_embedding: List[float],
        document_id: str,
        k: int = 5,
        threshold: float = 0.0
    ) -> List[RAGSearchResult]:
        """
        Search for similar chunks within a document.
        
        Uses cosine similarity for ranking.
        """
        if document_id not in self.embeddings:
            return []
        
        doc_embeddings = self.embeddings[document_id]
        query_vec = np.array(query_embedding)
        
        # Compute cosine similarities
        # Normalize vectors
        query_norm = query_vec / np.linalg.norm(query_vec)
        doc_norms = doc_embeddings / np.linalg.norm(doc_embeddings, axis=1, keepdims=True)
        
        similarities = np.dot(doc_norms, query_norm)
        
        # Get top k indices
        top_indices = np.argsort(similarities)[::-1][:k]
        
        results = []
        document = self.documents[document_id]
        
        for idx in top_indices:
            if idx < len(document.chunks):
                chunk = document.chunks[idx]
                score = float(similarities[idx])
                
                if score >= threshold:
                    results.append(RAGSearchResult(
                        content=chunk.content,
                        similarity_score=score,
                        page_number=chunk.page_number,
                        chunk_index=chunk.chunk_index
                    ))
        
        return results
    
    def get_document(self, document_id: str) -> Optional[RAGDocument]:
        """Get a document by ID"""
        return self.documents.get(document_id)
    
    def remove_document(self, document_id: str):
        """Remove a document"""
        if document_id in self.documents:
            doc = self.documents[document_id]
            for chunk in doc.chunks:
                self.chunks.pop(chunk.chunk_id, None)
            self.embeddings.pop(document_id, None)
            self.documents.pop(document_id)
    
    def clear(self):
        """Clear all documents"""
        self.documents.clear()
        self.embeddings.clear()
        self.chunks.clear()


# ============================================================================
# RAG SERVICE
# ============================================================================
class RAGService:
    """Main RAG service for LitScholar full-text analysis"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.pdf_extractor = PDFExtractor()
        self.chunker = TextChunker(chunk_size=500, chunk_overlap=50)
        self.embedding_service = EmbeddingService(api_key=api_key)
        self.vector_store = InMemoryVectorStore()
    
    async def process_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        pmid: Optional[str] = None
    ) -> RAGDocument:
        """
        Process a PDF document for RAG.
        
        1. Extract text from PDF
        2. Chunk the text
        3. Generate embeddings
        4. Store in vector store
        
        Returns RAGDocument with processing results.
        """
        # Generate document ID
        doc_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]
        document_id = f"{pmid or 'doc'}_{doc_hash}"
        
        # Check if already processed
        existing = self.vector_store.get_document(document_id)
        if existing:
            logger.info(f"Document {document_id} already processed")
            return existing
        
        # Extract text
        text, page_count = self.pdf_extractor.extract_from_bytes(pdf_bytes, filename)
        
        # Chunk text
        chunks = self.chunker.chunk_text(text, document_id)
        logger.info(f"Created {len(chunks)} chunks from {filename}")
        
        # Generate embeddings
        chunk_texts = [chunk.content for chunk in chunks]
        embeddings = self.embedding_service.embed_texts(chunk_texts)
        logger.info(f"Generated embeddings for {len(chunks)} chunks")
        
        # Create document
        document = RAGDocument(
            document_id=document_id,
            pmid=pmid,
            filename=filename,
            chunks=chunks,
            embeddings=embeddings,
            total_pages=page_count,
            total_chunks=len(chunks),
            created_at=datetime.now(timezone.utc).isoformat()
        )
        
        # Store in vector store
        self.vector_store.add_document(document)
        
        return document
    
    async def query_document(
        self,
        document_id: str,
        query: str,
        k: int = 5
    ) -> List[RAGSearchResult]:
        """
        Query a processed document using semantic search.
        
        Returns the top k most relevant chunks.
        """
        # Generate query embedding
        query_embedding = self.embedding_service.embed_query(query)
        
        # Search vector store
        results = self.vector_store.search(
            query_embedding=query_embedding,
            document_id=document_id,
            k=k,
            threshold=0.3  # Minimum similarity threshold
        )
        
        return results
    
    async def generate_answer(
        self,
        query: str,
        context_chunks: List[RAGSearchResult],
        article_title: Optional[str] = None,
        article_abstract: Optional[str] = None
    ) -> str:
        """
        Generate an answer using retrieved context.
        
        Uses GPT-5.2 via Emergent LLM Key to synthesize answer
        from the retrieved chunks.
        """
        try:
            from emergentintegrations.llm.openai import (
                OpenAIConfig,
                chat_completion,
                ChatMessage
            )
            
            # Build context from retrieved chunks
            context_text = "\n\n".join([
                f"[Page {r.page_number}, Section {r.chunk_index}]\n{r.content}"
                for r in context_chunks
            ])
            
            # Build system prompt
            system_prompt = """You are LitScholar, an AI assistant specialized in analyzing medical literature.

Your task is to answer questions about a specific research article using ONLY the provided context.

CRITICAL RULES:
1. ONLY use information from the provided context
2. If the answer is not in the context, say "I cannot find this information in the article"
3. NEVER make up information or use outside knowledge
4. Cite specific page numbers when possible
5. Be precise and evidence-based

Article Information:
"""
            if article_title:
                system_prompt += f"Title: {article_title}\n"
            if article_abstract:
                system_prompt += f"Abstract: {article_abstract[:500]}...\n"
            
            system_prompt += f"\nFull Text Context:\n{context_text}"
            
            # Generate answer
            config = OpenAIConfig(emergent_api_key=os.environ.get("EMERGENT_API_KEY"))
            
            messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=query)
            ]
            
            response = await chat_completion(
                config=config,
                messages=messages,
                model="gpt-5.2",
                temperature=0.3,  # Lower temperature for more factual answers
                max_tokens=1000
            )
            
            return response.content
            
        except ImportError as e:
            logger.error(f"emergentintegrations not available: {e}")
            # Return context-based summary as fallback
            return self._fallback_answer(query, context_chunks)
        except Exception as e:
            logger.error(f"Error generating answer: {e}")
            return self._fallback_answer(query, context_chunks)
    
    def _fallback_answer(self, query: str, chunks: List[RAGSearchResult]) -> str:
        """Fallback answer when LLM is not available"""
        if not chunks:
            return "I could not find relevant information in the document for your question."
        
        answer = f"Based on the document, here are the most relevant sections:\n\n"
        for i, chunk in enumerate(chunks[:3], 1):
            answer += f"**Section {i} (Page {chunk.page_number}):**\n{chunk.content[:300]}...\n\n"
        
        return answer
    
    def get_document_info(self, document_id: str) -> Optional[dict]:
        """Get information about a processed document"""
        doc = self.vector_store.get_document(document_id)
        if not doc:
            return None
        
        return {
            "document_id": doc.document_id,
            "pmid": doc.pmid,
            "filename": doc.filename,
            "total_pages": doc.total_pages,
            "total_chunks": doc.total_chunks,
            "created_at": doc.created_at
        }
    
    def remove_document(self, document_id: str):
        """Remove a processed document"""
        self.vector_store.remove_document(document_id)


# Global RAG service instance
_rag_service: Optional[RAGService] = None

def get_rag_service() -> RAGService:
    """Get or create the RAG service singleton"""
    global _rag_service
    if _rag_service is None:
        api_key = os.environ.get("EMERGENT_API_KEY")
        _rag_service = RAGService(api_key=api_key)
    return _rag_service
