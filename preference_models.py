from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Literal
from datetime import datetime

class ScheduleConfig(BaseModel):
    frequency: Literal["daily", "weekly", "biweekly", "monthly"]
    time_local: Optional[str] = None  # HH:MM format; computed from hour/minute for legacy docs
    timezone: Optional[str] = None  # IANA timezone (Optional to support dual-write from profiles)
    day_of_week: Optional[str] = None  # Mon, Tue, etc. for weekly/biweekly
    day_of_month: Optional[int] = None  # 1-31 for monthly
    # Legacy fields — kept for backward compat with older documents
    hour: Optional[int] = None
    minute: Optional[int] = None

    @model_validator(mode='after')
    def ensure_time_local(self):
        """Derive time_local from legacy hour/minute if not set."""
        if self.time_local is None:
            if self.hour is not None and self.minute is not None:
                self.time_local = f"{self.hour:02d}:{self.minute:02d}"
            else:
                self.time_local = "09:00"
        # Validate format
        try:
            parts = self.time_local.split(':')
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59) or len(parts) != 2:
                self.time_local = "09:00"
        except (ValueError, AttributeError, IndexError):
            self.time_local = "09:00"
        # Default timezone if not set
        if self.timezone is None:
            self.timezone = "UTC"
        return self
    
    @field_validator('day_of_month')
    @classmethod
    def validate_day(cls, v):
        if v is not None and not (1 <= v <= 31):
            raise ValueError('day_of_month must be between 1 and 31')
        return v

class AdvancedPreferences(BaseModel):
    clinical_notes: Optional[str] = None
    journal_notes: Optional[str] = None

class PreferenceMetadata(BaseModel):
    topics_priority: List[str] = []
    journals_priority: List[str] = []

class PreferenceCreate(BaseModel):
    specialty_id: str
    subspecialty_id: Optional[str] = None  # Kept for backward compatibility
    subspecialties: List[str] = []  # New: support multiple subspecialties
    topics_selected: List[str] = []
    custom_topics: List[str] = []
    journals_selected: List[str] = []
    custom_journals: List[str] = []
    max_articles_per_digest: int = Field(default=10, ge=5, le=20)
    schedule: ScheduleConfig
    metadata: Optional[PreferenceMetadata] = None
    advanced_preferences: Optional[AdvancedPreferences] = None
    # Email controls (backward compatible)
    email_notifications_enabled: bool = True
    email_suppress_until: Optional[str] = None
    # Phase A v2: Reading goal (optional, backward compatible)
    reading_goal_weekly: Optional[int] = Field(default=None, ge=0, le=100)

class PreferenceResponse(BaseModel):
    user_id: str
    specialty_id: str
    subspecialty_id: Optional[str] = None  # Kept for backward compatibility
    subspecialties: List[str] = []  # New: support multiple subspecialties
    topics_selected: List[str]
    custom_topics: List[str]
    journals_selected: List[str]
    custom_journals: List[str]
    max_articles_per_digest: int
    schedule: ScheduleConfig
    metadata: Optional[PreferenceMetadata] = None
    advanced_preferences: Optional[AdvancedPreferences] = None
    # Email controls (backward compatible)
    email_notifications_enabled: bool = True
    email_suppress_until: Optional[str] = None
    # Phase A v2: Reading goal (optional, backward compatible)
    reading_goal_weekly: Optional[int] = None
    last_run_timestamp: Optional[str] = None
    next_run_timestamp: Optional[str] = None
    is_active: bool
    created_at: str
    updated_at: str

class TestSearchRequest(BaseModel):
    days_back: int = Field(default=7, ge=1, le=30)
    max_results: int = Field(default=10, ge=1, le=50)
