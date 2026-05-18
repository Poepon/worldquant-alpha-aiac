"""
Knowledge Router - CoSTEER Knowledge Base management

Uses KnowledgeService for all business logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from backend.database import get_db
from backend.services.knowledge_service import (
    KnowledgeService,
    KnowledgeListFilters,
    KnowledgeCreateData,
    KnowledgeUpdateData,
)

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_knowledge_service(db: AsyncSession = Depends(get_db)) -> KnowledgeService:
    """Get KnowledgeService instance with injected dependencies."""
    return KnowledgeService(db)


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class KnowledgeEntryResponse(BaseModel):
    id: int
    entry_type: str
    pattern: Optional[str] = None
    description: Optional[str] = None
    meta_data: dict = {}
    usage_count: int
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class KnowledgeCreateRequest(BaseModel):
    entry_type: str  # SUCCESS_PATTERN, FAILURE_PITFALL, FIELD_BLACKLIST, OPERATOR_STAT
    pattern: Optional[str] = None
    description: Optional[str] = None
    meta_data: dict = {}


class KnowledgeUpdateRequest(BaseModel):
    pattern: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[dict] = None
    is_active: Optional[bool] = None


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("", response_model=List[KnowledgeEntryResponse])
async def list_knowledge(
    entry_type: Optional[str] = Query(None, description="Filter by type"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    region: Optional[str] = Query(None, description="Filter by meta_data.region"),
    created_by: Optional[str] = Query(None, description="SYSTEM/HITL/USER"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """List knowledge base entries with optional filters."""
    filters = KnowledgeListFilters(
        entry_type=entry_type,
        is_active=is_active,
        region=region,
        created_by=created_by,
        limit=limit,
        offset=offset,
    )

    entries = await service.list_entries(filters)

    return [
        KnowledgeEntryResponse(
            id=e.id,
            entry_type=e.entry_type,
            pattern=e.pattern,
            description=e.description,
            meta_data=e.meta_data,
            usage_count=e.usage_count,
            is_active=e.is_active,
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in entries
    ]


@router.get("/success-patterns", response_model=List[KnowledgeEntryResponse])
async def get_success_patterns(
    limit: int = Query(20, ge=1, le=100),
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Get successful alpha patterns for RAG retrieval."""
    entries = await service.get_success_patterns(limit=limit)
    
    return [
        KnowledgeEntryResponse(
            id=e.id,
            entry_type=e.entry_type,
            pattern=e.pattern,
            description=e.description,
            meta_data=e.meta_data,
            usage_count=e.usage_count,
            is_active=e.is_active,
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in entries
    ]


@router.get("/failure-pitfalls", response_model=List[KnowledgeEntryResponse])
async def get_failure_pitfalls(
    limit: int = Query(50, ge=1, le=100),
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Get failure pitfalls for the feedback loop."""
    entries = await service.get_failure_pitfalls(limit=limit)
    
    return [
        KnowledgeEntryResponse(
            id=e.id,
            entry_type=e.entry_type,
            pattern=e.pattern,
            description=e.description,
            meta_data=e.meta_data,
            usage_count=e.usage_count,
            is_active=e.is_active,
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in entries
    ]


@router.get("/field-blacklist", response_model=List[KnowledgeEntryResponse])
async def get_field_blacklist(
    region: Optional[str] = Query(None, description="Filter by region"),
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Get blacklisted fields that should not be used in alpha expressions."""
    entries = await service.get_field_blacklist(region=region)
    
    return [
        KnowledgeEntryResponse(
            id=e.id,
            entry_type=e.entry_type,
            pattern=e.pattern,
            description=e.description,
            meta_data=e.meta_data,
            usage_count=e.usage_count,
            is_active=e.is_active,
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in entries
    ]


@router.post("", response_model=KnowledgeEntryResponse)
async def create_knowledge_entry(
    request: KnowledgeCreateRequest,
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Create a new knowledge entry (manually add a pattern or pitfall)."""
    data = KnowledgeCreateData(
        entry_type=request.entry_type,
        pattern=request.pattern,
        description=request.description,
        meta_data=request.meta_data,
    )
    
    entry = await service.create_entry(data)
    
    return KnowledgeEntryResponse(
        id=entry.id,
        entry_type=entry.entry_type,
        pattern=entry.pattern,
        description=entry.description,
        meta_data=entry.meta_data,
        usage_count=entry.usage_count,
        is_active=entry.is_active,
        created_by=entry.created_by,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@router.put("/{entry_id}", response_model=KnowledgeEntryResponse)
async def update_knowledge_entry(
    entry_id: int,
    request: KnowledgeUpdateRequest,
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Update a knowledge entry."""
    data = KnowledgeUpdateData(
        pattern=request.pattern,
        description=request.description,
        meta_data=request.meta_data,
        is_active=request.is_active,
    )
    
    try:
        entry = await service.update_entry(entry_id, data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    return KnowledgeEntryResponse(
        id=entry.id,
        entry_type=entry.entry_type,
        pattern=entry.pattern,
        description=entry.description,
        meta_data=entry.meta_data,
        usage_count=entry.usage_count,
        is_active=entry.is_active,
        created_by=entry.created_by,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@router.delete("/{entry_id}")
async def delete_knowledge_entry(
    entry_id: int,
    service: KnowledgeService = Depends(get_knowledge_service),
):
    """Delete a knowledge entry (or deactivate it)."""
    try:
        await service.delete_entry(entry_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    return {"message": "Knowledge entry deactivated", "id": entry_id}
