"""
Verification Routes for LitPulse v2.1
Professional credential verification via work email
"""
from fastapi import APIRouter, HTTPException, Depends, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone, timedelta
import uuid
import logging
import secrets

from auth_utils import get_current_user
from subscription_utils import is_level_2_subscriber, get_verification_status, get_subscription_level
from verification_models import (
    VerificationSubmitRequest,
    VerificationStatusResponse,
    VerificationSubmitResponse,
    WorkEmailSendCodeRequest,
    WorkEmailCodeResponse,
    WorkEmailConfirmRequest,
    WorkEmailConfirmResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/verification", tags=["verification"])

# Database reference (set by main app)
db: AsyncIOMotorDatabase = None

def set_db(database: AsyncIOMotorDatabase):
    """Set the database reference for the router"""
    global db
    db = database

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def generate_verification_code() -> str:
    """Generate a 6-digit verification code"""
    return ''.join([str(secrets.randbelow(10)) for _ in range(6)])

def hash_verification_code(code: str) -> str:
    """Hash a verification code for secure storage using SHA-256"""
    import hashlib
    return hashlib.sha256(code.encode()).hexdigest()

def verify_code_hash(provided_code: str, stored_hash: str) -> bool:
    """Verify a provided code against a stored hash"""
    return hash_verification_code(provided_code) == stored_hash

# Free email providers - allowed but flagged as "personal_email" method (not auto-verified)
FREE_EMAIL_PROVIDERS = [
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'aol.com', 'icloud.com', 'mail.com', 'protonmail.com',
    'yandex.com', 'gmx.com', 'zoho.com', 'live.com',
    'msn.com', 'me.com', 'qq.com', '163.com', '126.com'
]

def is_free_email_provider(email: str) -> bool:
    """Check if email is from a free provider (not auto-verifiable)"""
    if not email or '@' not in email:
        return True
    domain = email.lower().split('@')[-1]
    return domain in FREE_EMAIL_PROVIDERS

def is_valid_work_email(email: str) -> bool:
    """
    Validate that email is from an institutional/work domain, not personal.
    Returns False for common free email providers.
    NOTE: Free emails are still ALLOWED for submission, but won't auto-verify.
    """
    free_providers = [
        'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
        'aol.com', 'icloud.com', 'mail.com', 'protonmail.com',
        'yandex.com', 'gmx.com', 'zoho.com', 'live.com',
        'msn.com', 'me.com', 'qq.com', '163.com', '126.com'
    ]
    
    if not email or '@' not in email:
        return False
    
    domain = email.lower().split('@')[-1]
    return domain not in free_providers

async def get_user_with_subscription(user_id: str) -> dict:
    """Get user document with subscription level"""
    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "user_id": 1, "email": 1, "full_name": 1, "subscription_level": 1}
    )
    return user

async def get_user_verification(user_id: str) -> dict:
    """Get user's verification document"""
    return await db.professional_verifications.find_one(
        {"user_id": user_id},
        {"_id": 0}
    )

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/me", response_model=VerificationStatusResponse)
async def get_my_verification_status(current_user: dict = Depends(get_current_user)):
    """Get current user's verification status (available to all authenticated users)"""
    try:
        user_id = current_user["user_id"]
        
        # Get verification document
        verification = await get_user_verification(user_id)
        
        if not verification:
            return VerificationStatusResponse(
                status="not_submitted",
                can_submit=True,
                message=None
            )
        
        return VerificationStatusResponse(
            status=verification.get("status", "not_submitted"),
            method=verification.get("method"),
            profession_role=verification.get("submitted_payload", {}).get("profession_role"),
            submitted_at=verification.get("submitted_at"),
            verified_at=verification.get("verified_at"),
            can_submit=verification.get("status") not in ["pending", "verified"],
            message=None
        )
        
    except Exception as e:
        logger.error(f"Get verification status error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get verification status"
        )

@router.post("/submit", response_model=VerificationSubmitResponse)
async def submit_verification(
    data: VerificationSubmitRequest,
    current_user: dict = Depends(get_current_user)
):
    """Submit professional verification (available to all authenticated users)"""
    try:
        user_id = current_user["user_id"]
        
        # Check existing verification
        existing = await get_user_verification(user_id)
        if existing and existing.get("status") == "verified":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Already verified"
            )
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Build verification document
        verification_doc = {
            "verification_id": str(uuid.uuid4()),
            "user_id": user_id,
            "status": "pending",
            "method": data.method,
            "submitted_payload": {
                "profession_role": data.profession_role,
                "country": data.country,
                "state": data.state,
                "license_number": data.license_number,
                "npi": data.npi,
                "work_email": data.work_email
            },
            "submitted_at": now,
            "updated_at": now,
            "verified_at": None,
            "reviewer_note": None
        }
        
        # Upsert verification document
        await db.professional_verifications.update_one(
            {"user_id": user_id},
            {"$set": verification_doc},
            upsert=True
        )
        
        logger.info(f"Verification submitted: user {user_id}, method {data.method}")
        
        # Determine next step based on method
        next_step = None
        if data.method == "work_email" and data.work_email:
            next_step = "Please verify your work email by requesting a verification code"
        elif data.method in ["license", "npi"]:
            next_step = "Your credentials will be reviewed manually. This may take 1-3 business days."
        
        return VerificationSubmitResponse(
            status="pending",
            message="Verification submitted successfully",
            next_step=next_step
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Submit verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit verification"
        )

@router.post("/work-email/send-code", response_model=WorkEmailCodeResponse)
async def send_work_email_code(
    data: WorkEmailSendCodeRequest,
    current_user: dict = Depends(get_current_user)
):
    """Send verification code to work email (available to all authenticated users)"""
    try:
        user_id = current_user["user_id"]
        
        # Get user info for email
        user = await get_user_with_subscription(user_id)
        
        work_email = data.work_email.lower()
        
        # Check if free email provider - allowed but won't auto-verify
        is_free_email = is_free_email_provider(work_email)
        if is_free_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Personal email addresses (Gmail, Yahoo, etc.) cannot be used for automatic verification. Please use your institutional or work email address. If you don't have access to a work email, contact support for manual verification options."
            )
        
        # Check existing verification
        existing = await get_user_verification(user_id)
        if existing and existing.get("status") == "verified":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are already verified."
            )
        
        # Rate limit: only allow one code per 2 minutes
        now = datetime.now(timezone.utc)
        if existing and existing.get("work_email_code_sent_at"):
            try:
                sent_at = datetime.fromisoformat(existing["work_email_code_sent_at"].replace("Z", "+00:00"))
                if now - sent_at < timedelta(minutes=2):
                    seconds_remaining = int((timedelta(minutes=2) - (now - sent_at)).total_seconds())
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Please wait {seconds_remaining} seconds before requesting a new code."
                    )
            except (ValueError, KeyError):
                pass  # Invalid date format, allow new code
        
        # Generate code with 15-minute expiry (security: shorter window = less risk)
        code = generate_verification_code()
        expires_at = now + timedelta(minutes=15)
        
        # Get user name for email
        user_name = user.get("full_name") or user.get("email", "").split("@")[0] or "there"
        
        # Hash the code before storing (security: never store plaintext codes)
        code_hash = hash_verification_code(code)
        
        # Store HASHED code in verification document
        await db.professional_verifications.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "work_email_code_hash": code_hash,  # Store hash, not plaintext
                    "work_email_code_expires": expires_at.isoformat(),
                    "work_email_code_sent_at": now.isoformat(),
                    "work_email_pending": work_email,
                    "code_attempts": 0,
                    "updated_at": now.isoformat()
                },
                "$unset": {
                    "work_email_code": ""  # Remove any legacy plaintext codes
                },
                "$setOnInsert": {
                    "verification_id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "status": "pending",
                    "method": "work_email",
                    "created_at": now.isoformat()
                }
            },
            upsert=True
        )
        
        # Send email via SendGrid (with PLAINTEXT code - only sent to user's email)
        try:
            from email_service import send_verification_code_email
            email_sent = send_verification_code_email(work_email, code, user_name)
            if not email_sent:
                logger.warning(f"Verification code email may not have been sent to {work_email}")
            else:
                logger.info(f"Verification code sent to {work_email} for user {user_id}")
        except Exception as email_error:
            logger.error(f"Failed to send verification email: {email_error}")
            # Don't fail the request - code is saved, admin can verify manually if needed
        
        # Mask email for response
        email_parts = work_email.split("@")
        masked_local = email_parts[0][:2] + "***" if len(email_parts[0]) > 2 else email_parts[0]
        masked_email = f"{masked_local}@{email_parts[1]}"
        
        return WorkEmailCodeResponse(
            message="Verification code sent to your work email. Code expires in 15 minutes.",
            email_sent_to=masked_email
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Send work email code error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification code"
        )

@router.post("/work-email/confirm", response_model=WorkEmailConfirmResponse)
async def confirm_work_email_code(
    data: WorkEmailConfirmRequest,
    current_user: dict = Depends(get_current_user)
):
    """Confirm work email verification code"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc)
        
        # Get verification document
        verification = await get_user_verification(user_id)
        if not verification:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No pending verification found. Please request a code first."
            )
        
        if verification.get("status") == "verified":
            return WorkEmailConfirmResponse(
                status="verified",
                message="You are already verified."
            )
        
        # Check code hash exists (support both old plaintext and new hash format)
        stored_code_hash = verification.get("work_email_code_hash")
        stored_code_plaintext = verification.get("work_email_code")  # Legacy support
        expires_str = verification.get("work_email_code_expires")
        
        if not (stored_code_hash or stored_code_plaintext) or not expires_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No verification code found. Please request a new code."
            )
        
        # Check attempt limit (max 5 attempts)
        attempts = verification.get("code_attempts", 0)
        if attempts >= 5:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts. Please request a new code."
            )
        
        # Check expiry
        expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        if now > expires_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Verification code has expired. Please request a new code."
            )
        
        # Validate code (check hash first, fall back to plaintext for legacy)
        provided_code = data.code.strip()
        code_valid = False
        
        if stored_code_hash:
            # New secure method: verify against hash
            code_valid = verify_code_hash(provided_code, stored_code_hash)
        elif stored_code_plaintext:
            # Legacy fallback: direct comparison (will be migrated on next code request)
            code_valid = (provided_code == stored_code_plaintext)
        
        if not code_valid:
            # Increment attempts
            await db.professional_verifications.update_one(
                {"user_id": user_id},
                {"$inc": {"code_attempts": 1}, "$set": {"updated_at": now.isoformat()}}
            )
            remaining = 4 - attempts
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid verification code. {remaining} attempt(s) remaining."
            )
        
        # Update to verified
        work_email = verification.get("work_email_pending")
        
        await db.professional_verifications.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "status": "verified",
                    "verified_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "method": "work_email",
                    "submitted_payload.work_email": work_email
                },
                "$unset": {
                    "work_email_code": "",        # Legacy plaintext field
                    "work_email_code_hash": "",   # New hashed field
                    "work_email_code_expires": "",
                    "work_email_code_sent_at": "",
                    "work_email_pending": "",
                    "code_attempts": ""
                }
            }
        )
        
        logger.info(f"User {user_id} verified via work email: {work_email}")
        
        return WorkEmailConfirmResponse(
            status="verified",
            message="Congratulations! Your professional credentials have been verified. Your Verified badge will now appear in discussions."
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Confirm work email code error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to confirm verification code"
        )



# ============================================================
# SUBSCRIPTION LEVEL ENDPOINT
# ============================================================

@router.get("/subscription-level")
async def get_subscription_level_endpoint(current_user: dict = Depends(get_current_user)):
    """Get current user's subscription level"""
    try:
        user_id = current_user["user_id"]
        
        user = await db.users.find_one(
            {"user_id": user_id}, 
            {"_id": 0, "subscription_level": 1}
        )
        
        level = get_subscription_level(user) if user else 1
        
        return {
            "subscription_level": level,
            "is_level_2": level >= 2
        }
        
    except Exception as e:
        logger.error(f"Get subscription level error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get subscription level"
        )



# ============================================================
# NPI VERIFICATION PLACEHOLDER
# ============================================================

def _validate_npi_format(npi: str) -> bool:
    """Validate NPI format: exactly 10 digits, optional Luhn check."""
    if not npi or len(npi) != 10 or not npi.isdigit():
        return False
    # Luhn check (NPI uses a modified Luhn algorithm with prefix 80840)
    digits = [int(d) for d in "80840" + npi]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


from pydantic import BaseModel as _NPIBase, Field as _NPIField

class NPISubmitRequest(_NPIBase):
    npi_number: str = _NPIField(..., min_length=10, max_length=10)


@router.post("/npi/submit")
async def submit_npi_verification(
    data: NPISubmitRequest,
    current_user: dict = Depends(get_current_user),
):
    """Submit NPI number for clinician verification.

    Placeholder: validates format, stores as pending.
    If ALLOW_NPI_SELF_ATTESTATION=true: auto-sets verified_provisional.
    PHI-Zero: NPI is stored but never logged.
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    user_id = current_user["user_id"]

    # Check if already verified
    existing = await db.professional_verifications.find_one(
        {"user_id": user_id}, {"_id": 0, "status": 1}
    )
    if existing and existing.get("status") in ("verified", "verified_provisional"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "already_verified", "message": "You are already verified."}
        )

    # Validate NPI format
    if not _validate_npi_format(data.npi_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "invalid_npi", "message": "Invalid NPI number. Must be 10 digits with valid checksum."}
        )

    now = datetime.now(timezone.utc).isoformat()
    allow_self_attest = flags.get("allow_npi_self_attestation", False)
    verification_status = "verified_provisional" if allow_self_attest else "pending"

    verification_doc = {
        "verification_id": str(uuid.uuid4()),
        "user_id": user_id,
        "method": "npi",
        "status": verification_status,
        "npi_number": data.npi_number,
        "submitted_at": now,
        "created_at": now,
        "updated_at": now,
    }

    if verification_status == "verified_provisional":
        verification_doc["verified_at"] = now

    # Upsert: replace any existing pending verification
    await db.professional_verifications.update_one(
        {"user_id": user_id},
        {"$set": verification_doc},
        upsert=True,
    )

    logger.info("NPI verification submitted: user=%s status=%s", user_id, verification_status)

    message = (
        "NPI verified (provisional). You now have clinician access."
        if verification_status == "verified_provisional"
        else "NPI submitted for review. You will be notified when verified."
    )

    return {
        "status": verification_status,
        "message": message,
        "method": "npi",
    }
