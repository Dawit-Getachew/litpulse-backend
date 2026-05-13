"""
Staging Trial Expiry Utility

Manually sets a user's trial_expires_at to the past (for expiry testing)
or restores it to 14 days from now.

Usage:
  # Expire trial (for testing "trial ended" state)
  python backend/scripts/staging_expire_trial.py --email smoketest@litpulse.com --expired

  # Restore trial to 14 days from now
  python backend/scripts/staging_expire_trial.py --email smoketest@litpulse.com --restore

  # Show current trial state
  python backend/scripts/staging_expire_trial.py --email smoketest@litpulse.com --status

IMPORTANT: For STAGING USE ONLY. Do not run against production DB.
"""
import sys
import os
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Staging trial expiry tool")
    parser.add_argument("--email", required=True, help="User email to modify")
    parser.add_argument("--expired", action="store_true", help="Set trial to expired (2 days ago)")
    parser.add_argument("--restore", action="store_true", help="Restore trial to 14 days from now")
    parser.add_argument("--status", action="store_true", help="Show current trial state")
    parser.add_argument("--mongo-url", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    parser.add_argument("--db-name", default=os.environ.get("DB_NAME", "litpulse_db"))
    args = parser.parse_args()

    if not (args.expired or args.restore or args.status):
        parser.error("Specify --expired, --restore, or --status")

    from pymongo import MongoClient
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=5000)
    db = client[args.db_name]

    # Safety guard: warn if looks like production
    if "prod" in args.db_name.lower() or "production" in args.db_name.lower():
        confirm = input(f"WARNING: DB name '{args.db_name}' looks like production. Type YES to continue: ")
        if confirm != "YES":
            print("Aborted.")
            sys.exit(1)

    user = db.users.find_one({"email": args.email.lower()}, {"_id": 0})
    if not user:
        print(f"User not found: {args.email}")
        sys.exit(1)

    if args.status:
        print(f"User: {user['email']}")
        print(f"  plan_tier:       {user.get('plan_tier', 'free')}")
        print(f"  trial_used:      {user.get('trial_used', False)}")
        print(f"  trial_started_at: {user.get('trial_started_at')}")
        print(f"  trial_expires_at: {user.get('trial_expires_at')}")
        expires = user.get("trial_expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                diff = (exp_dt - now).total_seconds() / 86400
                if diff > 0:
                    print(f"  trial_active:    True ({diff:.1f} days remaining)")
                else:
                    print(f"  trial_active:    False (expired {-diff:.1f} days ago)")
            except Exception:
                print(f"  trial_expires_at parse error")
        client.close()
        return

    now = datetime.now(timezone.utc)

    if args.expired:
        new_expires = (now - timedelta(days=2)).isoformat()
        db.users.update_one(
            {"email": args.email.lower()},
            {"$set": {"trial_expires_at": new_expires, "updated_at": now.isoformat()}},
        )
        print(f"Trial set to EXPIRED for {args.email}")
        print(f"  trial_expires_at = {new_expires} (2 days ago)")
        print("  → /api/auth/me should now show trial_active=false")
        print("  → /plan should show 'Trial ended' banner")

    elif args.restore:
        new_expires = (now + timedelta(days=14)).isoformat()
        db.users.update_one(
            {"email": args.email.lower()},
            {"$set": {"trial_expires_at": new_expires, "trial_used": True, "updated_at": now.isoformat()}},
        )
        print(f"Trial RESTORED for {args.email}")
        print(f"  trial_expires_at = {new_expires} (14 days from now)")
        print("  → /api/auth/me should now show trial_active=true")

    client.close()


if __name__ == "__main__":
    main()
