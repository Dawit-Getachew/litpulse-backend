#!/usr/bin/env python3
"""
Audio Migration Script: Local to S3 (Step 16)
Migrates existing local audio files to S3 storage.

Usage:
    python scripts/migrate_audio_local_to_s3.py --dry-run
    python scripts/migrate_audio_local_to_s3.py --execute

Features:
- Idempotent: skips files already on S3
- Verifies local file exists before upload
- Updates DB record with new storage_backend
- Provides summary report (counts only, no secrets)
"""
import asyncio
import argparse
import os
import sys
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from datetime import datetime, timezone

# Load environment
load_dotenv(Path(__file__).parent.parent / '.env')


async def migrate_local_to_s3(dry_run: bool = True):
    """Migrate local audio files to S3."""
    
    print("=" * 60)
    print("LitPulse Audio Migration: Local → S3")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    
    # Check S3 configuration
    bucket = os.environ.get("AUDIO_S3_BUCKET")
    if not bucket:
        print("\n❌ ERROR: AUDIO_S3_BUCKET not configured")
        return {"status": "error", "message": "S3 not configured"}
    
    access_key = os.environ.get("AUDIO_S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("AUDIO_S3_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        print("\n❌ ERROR: S3 credentials not configured")
        return {"status": "error", "message": "S3 credentials missing"}
    
    region = os.environ.get("AUDIO_S3_REGION", "us-east-1")
    endpoint_url = os.environ.get("AUDIO_S3_ENDPOINT_URL")
    local_dir = "/app/backend/storage/audio"
    
    print(f"\nConfiguration:")
    print(f"  S3 Bucket: {bucket[:6]}...{bucket[-4:] if len(bucket) > 10 else '***'}")
    print(f"  S3 Region: {region}")
    print(f"  Local Dir: {local_dir}")
    
    # Connect to MongoDB
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "litpulse_db")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    
    # Find audio records that need migration
    # (status=ready, storage_backend missing or "local")
    query = {
        "status": "ready",
        "$or": [
            {"storage_backend": {"$exists": False}},
            {"storage_backend": "local"},
        ]
    }
    
    records = await db.article_audio_summaries.find(query, {"_id": 0}).to_list(1000)
    
    print(f"\nRecords to migrate: {len(records)}")
    
    if not records:
        print("\n✅ No records need migration")
        return {"status": "ok", "migrated": 0, "skipped": 0, "errors": 0}
    
    # Initialize S3 client
    import boto3
    s3_kwargs = {
        "service_name": "s3",
        "region_name": region,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if endpoint_url:
        s3_kwargs["endpoint_url"] = endpoint_url
    s3 = boto3.client(**s3_kwargs)
    
    # Verify bucket access
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  S3 bucket verified: ✅")
    except Exception as e:
        print(f"\n❌ ERROR: Cannot access S3 bucket: {type(e).__name__}")
        return {"status": "error", "message": f"S3 access error: {type(e).__name__}"}
    
    # Migration stats
    stats = {
        "total": len(records),
        "migrated": 0,
        "skipped_no_file": 0,
        "skipped_already_s3": 0,
        "errors": 0,
    }
    
    print("\nProcessing records...")
    
    for i, record in enumerate(records):
        pmid = record.get("pmid", "unknown")
        storage_key = record.get("storage_key")
        text_hash = record.get("text_hash", "")[:8]
        voice = record.get("voice", "default")
        
        if not storage_key:
            stats["skipped_no_file"] += 1
            continue
        
        # Check if already on S3
        if record.get("storage_backend") == "s3":
            stats["skipped_already_s3"] += 1
            continue
        
        # Check local file exists
        local_path = os.path.join(local_dir, storage_key)
        if not os.path.exists(local_path):
            print(f"  [{i+1}/{len(records)}] PMID {pmid}: local file not found, skipping")
            stats["skipped_no_file"] += 1
            continue
        
        # Determine content type
        ext = storage_key.split(".")[-1] if "." in storage_key else "wav"
        content_type = "audio/mpeg" if ext == "mp3" else f"audio/{ext}"
        
        # S3 key pattern: audio/{pmid}/{text_hash}/{voice}.{ext}
        s3_key = f"audio/{pmid}/{text_hash}/{voice}.{ext}"
        
        if dry_run:
            print(f"  [{i+1}/{len(records)}] PMID {pmid}: would upload {storage_key} → {s3_key}")
            stats["migrated"] += 1
        else:
            try:
                # Read local file
                with open(local_path, "rb") as f:
                    file_data = f.read()
                
                # Upload to S3
                s3.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=file_data,
                    ContentType=content_type,
                )
                
                # Update DB record
                await db.article_audio_summaries.update_one(
                    {"pmid": pmid, "voice": voice, "text_hash": record.get("text_hash")},
                    {"$set": {
                        "storage_key": s3_key,
                        "storage_backend": "s3",
                        "file_size_bytes": len(file_data),
                        "migrated_at": datetime.now(timezone.utc).isoformat(),
                    }}
                )
                
                print(f"  [{i+1}/{len(records)}] PMID {pmid}: ✅ migrated to S3")
                stats["migrated"] += 1
                
            except Exception as e:
                print(f"  [{i+1}/{len(records)}] PMID {pmid}: ❌ error: {type(e).__name__}")
                stats["errors"] += 1
    
    # Summary
    print("\n" + "=" * 60)
    print("Migration Summary")
    print("=" * 60)
    print(f"  Total records:       {stats['total']}")
    print(f"  Migrated to S3:      {stats['migrated']}")
    print(f"  Skipped (no file):   {stats['skipped_no_file']}")
    print(f"  Skipped (already S3):{stats['skipped_already_s3']}")
    print(f"  Errors:              {stats['errors']}")
    
    if dry_run:
        print("\n⚠️  DRY RUN - No changes were made")
        print("    Run with --execute to perform actual migration")
    else:
        print(f"\n✅ Migration complete")
    
    return {"status": "ok", **stats}


def main():
    parser = argparse.ArgumentParser(description="Migrate local audio files to S3")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without executing")
    parser.add_argument("--execute", action="store_true", help="Execute the migration")
    
    args = parser.parse_args()
    
    if not args.dry_run and not args.execute:
        print("Please specify --dry-run or --execute")
        parser.print_help()
        sys.exit(1)
    
    dry_run = args.dry_run or not args.execute
    
    result = asyncio.run(migrate_local_to_s3(dry_run=dry_run))
    
    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
