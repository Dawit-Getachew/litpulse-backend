"""
Article Metadata Sidecar — Tags & Collections

Additive-only endpoints for user-scoped tags and collections on library articles.
These are sidecar data — they do NOT modify the saved-article write path,
the articles collection, or the library save/remove behavior.

Gated behind ENABLE_ARTICLE_NOTES feature flag.

Collections:
  - article_tags: {user_id, pmid, tags[]}
  - user_collections: {user_id, collection_id, name, created_at}
  - article_collections: {user_id, pmid, collection_id}
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase

from auth_utils import get_current_user
from utils.feature_flags import get_feature_flags

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/article-metadata", tags=["article-metadata"])
db: Optional[AsyncIOMotorDatabase] = None


def set_db(database: AsyncIOMotorDatabase):
    global db
    db = database


def _check_flag():
    flags = get_feature_flags()
    if not flags.get("enable_article_notes"):
        raise HTTPException(status_code=404, detail="Feature not enabled")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

class SetTagsRequest(BaseModel):
    pmid: str
    tags: List[str] = Field(default_factory=list, max_length=20)


class TagsResponse(BaseModel):
    pmid: str
    tags: List[str]


@router.get("/tags/{pmid}", response_model=TagsResponse)
async def get_tags(pmid: str, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    doc = await db.article_tags.find_one(
        {"user_id": user_id, "pmid": pmid}, {"_id": 0, "pmid": 1, "tags": 1}
    )
    return TagsResponse(pmid=pmid, tags=doc.get("tags", []) if doc else [])


@router.put("/tags", response_model=TagsResponse)
async def set_tags(data: SetTagsRequest, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    # Clean and deduplicate tags
    clean_tags = list(dict.fromkeys(t.strip()[:50] for t in data.tags if t.strip()))[:20]
    await db.article_tags.update_one(
        {"user_id": user_id, "pmid": data.pmid},
        {"$set": {"tags": clean_tags, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return TagsResponse(pmid=data.pmid, tags=clean_tags)


@router.get("/tags", response_model=List[TagsResponse])
async def get_all_tags(current_user: dict = Depends(get_current_user)):
    """Get all tags for all articles for this user (for tag cloud/filter)."""
    _check_flag()
    user_id = current_user["user_id"]
    docs = await db.article_tags.find(
        {"user_id": user_id, "tags": {"$ne": []}},
        {"_id": 0, "pmid": 1, "tags": 1},
    ).to_list(500)
    return [TagsResponse(pmid=d["pmid"], tags=d.get("tags", [])) for d in docs]


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

class CreateCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class RenameCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class CollectionResponse(BaseModel):
    collection_id: str
    name: str
    article_count: int = 0
    created_at: str


class AssignCollectionRequest(BaseModel):
    pmid: str
    collection_id: str


@router.get("/collections", response_model=List[CollectionResponse])
async def list_collections(current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    cols = await db.user_collections.find(
        {"user_id": user_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(50)

    result = []
    for c in cols:
        count = await db.article_collections.count_documents(
            {"user_id": user_id, "collection_id": c["collection_id"]}
        )
        result.append(CollectionResponse(
            collection_id=c["collection_id"],
            name=c["name"],
            article_count=count,
            created_at=c.get("created_at", ""),
        ))
    return result


@router.post("/collections", response_model=CollectionResponse, status_code=201)
async def create_collection(data: CreateCollectionRequest, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    col_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.user_collections.insert_one({
        "collection_id": col_id,
        "user_id": user_id,
        "name": data.name.strip(),
        "created_at": now,
    })
    return CollectionResponse(collection_id=col_id, name=data.name.strip(), article_count=0, created_at=now)


@router.put("/collections/{collection_id}")
async def rename_collection(collection_id: str, data: RenameCollectionRequest, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    result = await db.user_collections.update_one(
        {"collection_id": collection_id, "user_id": user_id},
        {"$set": {"name": data.name.strip()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"message": "Renamed"}


@router.delete("/collections/{collection_id}", status_code=204)
async def delete_collection(collection_id: str, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    await db.user_collections.delete_one({"collection_id": collection_id, "user_id": user_id})
    await db.article_collections.delete_many({"collection_id": collection_id, "user_id": user_id})


@router.post("/collections/assign")
async def assign_to_collection(data: AssignCollectionRequest, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    await db.article_collections.update_one(
        {"user_id": user_id, "pmid": data.pmid, "collection_id": data.collection_id},
        {"$set": {"assigned_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"message": "Assigned"}


@router.delete("/collections/assign/{collection_id}/{pmid}")
async def unassign_from_collection(collection_id: str, pmid: str, current_user: dict = Depends(get_current_user)):
    _check_flag()
    user_id = current_user["user_id"]
    await db.article_collections.delete_one(
        {"user_id": user_id, "pmid": pmid, "collection_id": collection_id}
    )
    return {"message": "Removed"}


@router.get("/collections/article/{pmid}")
async def get_article_collections(pmid: str, current_user: dict = Depends(get_current_user)):
    """Get which collections an article belongs to."""
    _check_flag()
    user_id = current_user["user_id"]
    docs = await db.article_collections.find(
        {"user_id": user_id, "pmid": pmid}, {"_id": 0, "collection_id": 1}
    ).to_list(50)
    return {"pmid": pmid, "collection_ids": [d["collection_id"] for d in docs]}
