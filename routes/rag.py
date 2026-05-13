"""
RAG API Routes for LitScholar Full-Text Analysis

Endpoints for PDF upload, processing, and RAG-based Q&A.
"""
import logging
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from pydantic import BaseModel

from services.rag_service import get_rag_service, RAGService, RAGSearchResult
from server import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["RAG"])


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================
class PDFUploadResponse(BaseModel):
    """Response after PDF upload and processing"""
    success: bool
    document_id: str
    filename: str
    total_pages: int
    total_chunks: int
    message: str


class RAGQueryRequest(BaseModel):
    """Request for RAG-based Q&A"""
    document_id: str
    query: str
    article_title: Optional[str] = None
    article_abstract: Optional[str] = None
    k: int = 5  # Number of chunks to retrieve


class RAGQueryResponse(BaseModel):
    """Response from RAG Q&A"""
    answer: str
    sources: List[dict]
    document_id: str


class DocumentInfoResponse(BaseModel):
    """Information about a processed document"""
    document_id: str
    pmid: Optional[str]
    filename: str
    total_pages: int
    total_chunks: int
    created_at: str


# ============================================================================
# DEPENDENCY
# ============================================================================
def get_rag() -> RAGService:
    """Dependency to get RAG service"""
    return get_rag_service()


# ============================================================================
# ENDPOINTS
# ============================================================================
@router.post("/upload-pdf", response_model=PDFUploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    pmid: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag)
):
    """
    Upload and process a PDF for RAG analysis.
    
    - Extracts text from the PDF
    - Chunks the text into segments
    - Generates embeddings for semantic search
    - Stores in session-based vector store
    
    Returns document_id to use for queries.
    """
    # Validate file type
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported"
        )
    
    # Check file size (max 20MB)
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="PDF file too large. Maximum size is 20MB."
        )
    
    try:
        # Process the PDF
        document = await rag_service.process_pdf(
            pdf_bytes=content,
            filename=file.filename,
            pmid=pmid
        )
        
        logger.info(
            f"User {current_user['user_id']} uploaded PDF: {file.filename}, "
            f"document_id: {document.document_id}, chunks: {document.total_chunks}"
        )
        
        return PDFUploadResponse(
            success=True,
            document_id=document.document_id,
            filename=document.filename,
            total_pages=document.total_pages,
            total_chunks=document.total_chunks,
            message=f"PDF processed successfully. {document.total_chunks} text segments ready for analysis."
        )
        
    except ValueError as e:
        logger.error(f"PDF processing failed: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail=f"Could not process PDF: {str(e)}"
        )
    except Exception as e:
        logger.error(f"PDF upload error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to process PDF. Please try again."
        )


@router.post("/query", response_model=RAGQueryResponse)
async def rag_query(
    request: RAGQueryRequest,
    current_user: dict = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag)
):
    """
    Query a processed document using RAG.
    
    - Retrieves relevant chunks using semantic search
    - Generates answer using GPT with retrieved context
    - Returns answer with source citations
    """
    # Check if document exists
    doc_info = rag_service.get_document_info(request.document_id)
    if not doc_info:
        raise HTTPException(
            status_code=404,
            detail="Document not found. Please upload the PDF first."
        )
    
    try:
        # Retrieve relevant chunks
        chunks = await rag_service.query_document(
            document_id=request.document_id,
            query=request.query,
            k=request.k
        )
        
        if not chunks:
            return RAGQueryResponse(
                answer="I could not find relevant information in the document for your question. Try rephrasing or asking about a different topic covered in the article.",
                sources=[],
                document_id=request.document_id
            )
        
        # Generate answer using LLM
        answer = await rag_service.generate_answer(
            query=request.query,
            context_chunks=chunks,
            article_title=request.article_title,
            article_abstract=request.article_abstract
        )
        
        # Format sources
        sources = [
            {
                "page": chunk.page_number,
                "section": chunk.chunk_index,
                "relevance": round(chunk.similarity_score, 3),
                "excerpt": chunk.content[:200] + "..." if len(chunk.content) > 200 else chunk.content
            }
            for chunk in chunks
        ]
        
        logger.info(
            f"RAG query by {current_user['user_id']}: "
            f"doc={request.document_id}, chunks_retrieved={len(chunks)}"
        )
        
        return RAGQueryResponse(
            answer=answer,
            sources=sources,
            document_id=request.document_id
        )
        
    except Exception as e:
        logger.error(f"RAG query error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to process query. Please try again."
        )


@router.get("/document/{document_id}", response_model=DocumentInfoResponse)
async def get_document_info(
    document_id: str,
    current_user: dict = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag)
):
    """Get information about a processed document"""
    doc_info = rag_service.get_document_info(document_id)
    if not doc_info:
        raise HTTPException(
            status_code=404,
            detail="Document not found"
        )
    
    return DocumentInfoResponse(**doc_info)


@router.delete("/document/{document_id}")
async def delete_document(
    document_id: str,
    current_user: dict = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag)
):
    """Remove a processed document from the session"""
    doc_info = rag_service.get_document_info(document_id)
    if not doc_info:
        raise HTTPException(
            status_code=404,
            detail="Document not found"
        )
    
    rag_service.remove_document(document_id)
    
    return {"success": True, "message": "Document removed"}


@router.post("/search", response_model=List[dict])
async def search_chunks(
    document_id: str = Form(...),
    query: str = Form(...),
    k: int = Form(5),
    current_user: dict = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag)
):
    """
    Search for relevant chunks without generating an answer.
    
    Useful for debugging or showing source previews.
    """
    doc_info = rag_service.get_document_info(document_id)
    if not doc_info:
        raise HTTPException(
            status_code=404,
            detail="Document not found"
        )
    
    chunks = await rag_service.query_document(
        document_id=document_id,
        query=query,
        k=k
    )
    
    return [
        {
            "content": chunk.content,
            "page": chunk.page_number,
            "section": chunk.chunk_index,
            "relevance": round(chunk.similarity_score, 3)
        }
        for chunk in chunks
    ]
