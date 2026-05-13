"""
Verification Models for LitPulse v2.1
Professional credential verification system
"""
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, Literal
from datetime import datetime

# Verification status types
VerificationStatus = Literal["not_submitted", "pending", "verified", "rejected", "verified_provisional"]
VerificationMethod = Literal["work_email", "license", "npi", "manual"]

# ============================================================
# REQUEST MODELS
# ============================================================

class VerificationSubmitRequest(BaseModel):
    """Request model for submitting verification"""
    method: VerificationMethod
    profession_role: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    license_number: Optional[str] = Field(None, max_length=100)
    npi: Optional[str] = Field(None, max_length=20)
    work_email: Optional[EmailStr] = None

class WorkEmailSendCodeRequest(BaseModel):
    """Request model for sending work email verification code"""
    work_email: EmailStr

class WorkEmailConfirmRequest(BaseModel):
    """Request model for confirming work email verification code"""
    code: str = Field(..., min_length=6, max_length=10)

# ============================================================
# RESPONSE MODELS
# ============================================================

class VerificationStatusResponse(BaseModel):
    """Response model for verification status"""
    status: VerificationStatus
    method: Optional[VerificationMethod] = None
    profession_role: Optional[str] = None
    submitted_at: Optional[str] = None
    verified_at: Optional[str] = None
    can_submit: bool = True
    message: Optional[str] = None

class VerificationSubmitResponse(BaseModel):
    """Response model for verification submission"""
    status: VerificationStatus
    message: str
    next_step: Optional[str] = None

class WorkEmailCodeResponse(BaseModel):
    """Response model for work email code sent"""
    message: str
    email_sent_to: str

class WorkEmailConfirmResponse(BaseModel):
    """Response model for work email confirmation"""
    status: VerificationStatus
    message: str

# ============================================================
# INTERNAL MODELS (for DB operations)
# ============================================================

class SubmittedPayload(BaseModel):
    """Internal model for verification payload stored in DB"""
    profession_role: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    license_number: Optional[str] = None
    npi: Optional[str] = None
    work_email: Optional[str] = None
