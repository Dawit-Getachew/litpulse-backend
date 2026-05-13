from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
import pytz
import logging

logger = logging.getLogger(__name__)

def compute_next_run(
    current_utc: datetime,
    schedule: Dict,
    last_run: Optional[datetime] = None
) -> datetime:
    """
    Compute the next run timestamp in UTC based on schedule
    
    Args:
        current_utc: Current time in UTC
        schedule: Schedule configuration dict
        last_run: Last run timestamp (optional)
    
    Returns:
        Next run timestamp in UTC
    """
    
    frequency = schedule.get("frequency")
    time_local = schedule.get("time_local")  # HH:MM
    tz_name = schedule.get("timezone", "UTC")
    
    try:
        tz = pytz.timezone(tz_name)
    except:
        logger.warning(f"Invalid timezone {tz_name}, using UTC")
        tz = pytz.UTC
    
    # Parse time
    try:
        hour, minute = map(int, time_local.split(':'))
    except:
        logger.warning(f"Invalid time_local {time_local}, using 09:00")
        hour, minute = 9, 0
    
    # Convert current UTC to user's timezone
    current_local = current_utc.astimezone(tz)
    
    # Start with today at the specified time
    next_local = current_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # If that time has passed today, move to next occurrence
    if next_local <= current_local:
        if frequency == "daily":
            next_local += timedelta(days=1)
        elif frequency == "weekly":
            next_local += timedelta(days=7)
        elif frequency == "biweekly":
            next_local += timedelta(days=14)
        elif frequency == "monthly":
            # Add one month
            if next_local.month == 12:
                next_local = next_local.replace(year=next_local.year + 1, month=1)
            else:
                next_local = next_local.replace(month=next_local.month + 1)
            
            # Handle day_of_month if specified
            day_of_month = schedule.get("day_of_month")
            if day_of_month:
                try:
                    next_local = next_local.replace(day=min(day_of_month, 28))
                except:
                    pass
    
    # Handle day_of_week for weekly/biweekly
    if frequency in ["weekly", "biweekly"]:
        day_of_week = schedule.get("day_of_week")
        if day_of_week:
            target_weekday = _parse_weekday(day_of_week)
            current_weekday = next_local.weekday()
            
            if target_weekday != current_weekday:
                days_ahead = (target_weekday - current_weekday) % 7
                if days_ahead == 0:
                    days_ahead = 7 if frequency == "weekly" else 14
                next_local += timedelta(days=days_ahead)
    
    # Convert back to UTC
    next_utc = next_local.astimezone(timezone.utc)
    
    return next_utc

def _parse_weekday(day_str: str) -> int:
    """Parse weekday string to int (0=Monday, 6=Sunday)"""
    weekdays = {
        "mon": 0, "monday": 0,
        "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6
    }
    return weekdays.get(day_str.lower(), 0)

def get_date_window(days_back: int = 7) -> tuple:
    """Get start and end dates for search window"""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)
    return start_date, end_date
