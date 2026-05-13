from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, SandBoxMode
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env file before reading environment variables
load_dotenv(Path(__file__).parent / '.env')

logger = logging.getLogger(__name__)

# Configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@scienthesis.ai')
SENDGRID_FROM_NAME = os.environ.get('SENDGRID_FROM_NAME', 'Scienthesis')
APP_BASE_URL = os.environ.get('APP_BASE_URL', '')

def _send_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str,
    email_type: str
) -> bool:
    """Internal function to send email via SendGrid"""
    try:
        logger.info(f"[{email_type}] Preparing to send email to {to_email}")
        
        # Check if SendGrid is configured
        if not SENDGRID_API_KEY or SENDGRID_API_KEY == '':
            logger.warning(f"[{email_type}] SendGrid API key not configured. Email not sent.")
            return False
        
        # Create message
        message = Mail(
            from_email=(SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME),
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
            plain_text_content=text_content
        )
        
        # Disable sandbox mode for production
        message.mail_settings = SandBoxMode(enable=False)
        
        # Send email
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        
        logger.info(f"[{email_type}] Email sent successfully to {to_email}. Status: {response.status_code}")
        return True
        
    except Exception as e:
        logger.error(f"[{email_type}] Failed to send email to {to_email}: {str(e)}")
        return False

def send_verification_email(email: str, name: str, token: str) -> bool:
    """Send email verification link"""
    verification_link = f"{APP_BASE_URL}/verify-email?token={token}"
    
    subject = "Verify Your Email - Scienthesis"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #2563eb; color: white; padding: 20px; text-align: center; }}
            .content {{ background-color: #f9fafb; padding: 30px; }}
            .button {{ display: inline-block; padding: 12px 30px; background-color: #2563eb; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
            .footer {{ text-align: center; padding: 20px; font-size: 12px; color: #6b7280; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Welcome to Scienthesis</h1>
            </div>
            <div class="content">
                <h2>Hello {name or 'there'},</h2>
                <p>Thank you for signing up for Scienthesis, your personalized literature digest platform.</p>
                <p>To complete your registration, please verify your email address by clicking the button below:</p>
                <div style="text-align: center;">
                    <a href="{verification_link}" class="button">Verify Email Address</a>
                </div>
                <p>Or copy and paste this link into your browser:</p>
                <p style="word-break: break-all; color: #2563eb;">{verification_link}</p>
                <p><strong>This link will expire in 24 hours.</strong></p>
                <p>If you didn't create an account with Scienthesis, you can safely ignore this email.</p>
            </div>
            <div class="footer">
                <p>&copy; 2025 Scienthesis. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Welcome to Scienthesis!
    
    Hello {name or 'there'},
    
    Thank you for signing up. Please verify your email address by visiting:
    
    {verification_link}
    
    This link will expire in 24 hours.
    
    If you didn't create an account, you can ignore this email.
    
    Best regards,
    The Scienthesis Team
    """
    
    return _send_email(email, subject, html_content, text_content, "VERIFICATION")

def send_password_reset_email(email: str, name: str, token: str) -> bool:
    """Send password reset link"""
    reset_link = f"{APP_BASE_URL}/reset-password?token={token}"
    
    subject = "Reset Your Password - Scienthesis"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #dc2626; color: white; padding: 20px; text-align: center; }}
            .content {{ background-color: #f9fafb; padding: 30px; }}
            .button {{ display: inline-block; padding: 12px 30px; background-color: #dc2626; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
            .warning {{ background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0; }}
            .footer {{ text-align: center; padding: 20px; font-size: 12px; color: #6b7280; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Password Reset Request</h1>
            </div>
            <div class="content">
                <h2>Hello {name or 'there'},</h2>
                <p>We received a request to reset your Scienthesis account password.</p>
                <p>Click the button below to create a new password:</p>
                <div style="text-align: center;">
                    <a href="{reset_link}" class="button">Reset Password</a>
                </div>
                <p>Or copy and paste this link into your browser:</p>
                <p style="word-break: break-all; color: #dc2626;">{reset_link}</p>
                <div class="warning">
                    <strong>⚠️ Important:</strong> This link will expire in 1 hour for security reasons.
                </div>
                <p>If you didn't request a password reset, please ignore this email. Your password will remain unchanged.</p>
            </div>
            <div class="footer">
                <p>&copy; 2025 Scienthesis. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Password Reset Request
    
    Hello {name or 'there'},
    
    We received a request to reset your password. Visit this link to create a new password:
    
    {reset_link}
    
    IMPORTANT: This link will expire in 1 hour.
    
    If you didn't request this, ignore this email.
    
    Best regards,
    The Scienthesis Team
    """
    
    return _send_email(email, subject, html_content, text_content, "PASSWORD_RESET")

def send_digest_email(email: str, full_name: str, specialty_name: str, subspecialty_name: str, articles: list, digest_date: str, digest_id: str = None) -> bool:
    """Send literature digest email with deep links for in-app engagement"""
    
    subject = "Your LitPulse Queue is Ready for Screening"
    
    # Build articles HTML with deep links
    articles_html = ""
    for i, article in enumerate(articles[:15], 1):  # Max 15 articles
        key_findings = article.get("key_findings", "Key findings not available")
        if isinstance(key_findings, list):
            findings_html = "<ul>" + "".join([f"<li>{f}</li>" for f in key_findings]) + "</ul>"
        else:
            findings_html = f"<p>{key_findings}</p>"
        
        preferred_badge = ""
        if article.get("is_preferred_journal"):
            preferred_badge = '<span style="background-color: #dbeafe; color: #1e40af; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-left: 10px;">★ Preferred Journal</span>'
        
        article_title = article.get('title', 'No title')
        article_journal = article.get('journal', 'Unknown journal')
        article_date = article.get('pub_date', 'Unknown date')
        article_authors = article.get('authors', 'Unknown authors')[:100]
        article_summary = article.get('ai_summary', 'Summary not available')[:300]
        article_url = article.get('url', '#')
        article_id = article.get('pmid', '')
        
        # Build deep link URLs for in-app engagement (Phase A v2)
        open_in_app_url = f"{APP_BASE_URL}/deeplink?type=article&id={article_id}"
        save_to_library_url = f"{APP_BASE_URL}/deeplink?type=article&id={article_id}&action=save"
        if digest_id:
            open_in_app_url += f"&digest={digest_id}"
            save_to_library_url += f"&digest={digest_id}"
        
        articles_html += f"""
        <div style="background-color: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
            <h3 style="color: #1f2937; margin-top: 0;">{i}. {article_title}</h3>
            <p style="color: #6b7280; font-size: 14px; margin: 5px 0;">
                <strong>{article_journal}</strong> {preferred_badge}<br>
                {article_date} • {article_authors}
            </p>
            <div style="background-color: #f0f9ff; border-left: 4px solid #3b82f6; padding: 12px; margin: 10px 0;">
                <strong style="color: #1e40af;">Key Findings:</strong>
                {findings_html}
            </div>
            <p style="font-size: 14px; color: #374151; line-height: 1.6;">{article_summary}...</p>
            <div style="margin-top: 15px; display: flex; gap: 10px; flex-wrap: wrap;">
                <a href="{open_in_app_url}" style="display: inline-block; padding: 8px 16px; background-color: #2563eb; color: white; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: 500;">📱 Open in App</a>
                <a href="{save_to_library_url}" style="display: inline-block; padding: 8px 16px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: 500;">💾 Save to Library</a>
                <a href="{article_url}" style="display: inline-block; padding: 8px 16px; background-color: #6b7280; color: white; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: 500;">🔗 PubMed</a>
            </div>
        </div>
        """
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; background-color: #f3f4f6; }}
            .container {{ max-width: 700px; margin: 0 auto; background-color: #ffffff; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px 20px; text-align: center; }}
            .content {{ padding: 30px 20px; }}
            .footer {{ background-color: #f9fafb; text-align: center; padding: 20px; font-size: 12px; color: #6b7280; border-top: 1px solid #e5e7eb; }}
            .summary-box {{ background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin: 0; font-size: 28px;">Your LitPulse Queue is Ready</h1>
                <p style="margin: 10px 0 0 0; font-size: 16px; opacity: 0.9;">{specialty_name} • {subspecialty_name}</p>
            </div>
            <div class="content">
                <p style="font-size: 16px;">Hello <strong>{full_name}</strong>,</p>
                <p>Your customized literature queue is ready. Screen and select the best articles for your library — <strong>LitHub</strong>.</p>
                <p>Here are {len(articles)} new articles curated for your interests:</p>
                
                {articles_html}
                
                <div class="summary-box">
                    <p style="margin: 0;"><strong>Tip:</strong> You can adjust your queue frequency and topics anytime in your preferences.</p>
                </div>
            </div>
            <div class="footer">
                <p>You're receiving this based on your {specialty_name} preferences.</p>
                <p><a href="{APP_BASE_URL}/preferences" style="color: #2563eb;">Update Preferences</a> | <a href="{APP_BASE_URL}/library" style="color: #2563eb;">My Library</a></p>
                <p>&copy; 2025 Scienthesis. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    # Simple text version with deep links
    def build_article_text(i, article):
        article_id = article.get('pmid', '')
        open_url = f"{APP_BASE_URL}/deeplink?type=article&id={article_id}"
        save_url = f"{APP_BASE_URL}/deeplink?type=article&id={article_id}&action=save"
        return (
            f"{i}. {article.get('title', 'No title')}\n"
            f"   {article.get('journal', 'Unknown')} • {article.get('pub_date', 'Unknown date')}\n"
            f"   Open in App: {open_url}\n"
            f"   Save to Library: {save_url}\n"
            f"   PubMed: {article.get('url', '')}\n"
            f"   Key findings: {article.get('key_findings', 'Not available')}"
        )
    
    articles_text = "\n\n".join([
        build_article_text(i, article) for i, article in enumerate(articles[:15], 1)
    ])
    
    text_content = f"""
    Your LitPulse Queue is Ready for Screening
    {specialty_name} - {subspecialty_name}
    {digest_date}
    
    Hello {full_name},
    
    Your customized literature queue is ready. Screen and select the best articles for your library - LitHub.
    
    Here are {len(articles)} new articles curated for your interests:
    
    {articles_text}
    
    Update your preferences: {APP_BASE_URL}/preferences
    View your library: {APP_BASE_URL}/library
    
    Best regards,
    The LitPulse Team
    """
    
    return _send_email(email, subject, html_content, text_content, "DIGEST")



def send_verification_code_email(to_email: str, code: str, user_name: str = "there") -> bool:
    """Send professional verification code email"""
    
    subject = "LitPulse - Professional Verification Code"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #059669; color: white; padding: 20px; text-align: center; }}
            .content {{ background-color: #f9fafb; padding: 30px; }}
            .code-box {{ background-color: #d1fae5; border: 2px dashed #059669; padding: 20px; text-align: center; margin: 20px 0; border-radius: 8px; }}
            .code {{ font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #065f46; font-family: monospace; }}
            .footer {{ text-align: center; padding: 20px; font-size: 12px; color: #6b7280; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Professional Verification</h1>
            </div>
            <div class="content">
                <h2>Hello {user_name},</h2>
                <p>You've requested to verify your professional credentials using your work email address.</p>
                <p>Your verification code is:</p>
                <div class="code-box">
                    <span class="code">{code}</span>
                </div>
                <p><strong>This code expires in 15 minutes.</strong></p>
                <p>Enter this code in LitPulse to complete your verification and receive your Verified badge.</p>
                <p>If you didn't request this verification, you can safely ignore this email.</p>
            </div>
            <div class="footer">
                <p>&copy; 2025 LitPulse. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Professional Verification
    
    Hello {user_name},
    
    Your verification code is: {code}
    
    This code expires in 15 minutes.
    
    Enter this code in LitPulse to complete your professional verification.
    
    If you didn't request this, ignore this email.
    
    - The LitPulse Team
    """
    
    return _send_email(to_email, subject, html_content, text_content, "VERIFICATION_CODE")


def send_signup_verification_code_email(to_email: str, code: str, user_name: str = "there") -> bool:
    """Send 6-digit verification code for email verification during signup"""
    
    subject = "LitPulse - Verify Your Email"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #0ea5e9 0%, #3b82f6 100%); color: white; padding: 25px; text-align: center; border-radius: 8px 8px 0 0; }}
            .content {{ background-color: #f9fafb; padding: 30px; }}
            .code-box {{ background-color: #e0f2fe; border: 2px solid #0ea5e9; padding: 24px; text-align: center; margin: 24px 0; border-radius: 12px; }}
            .code {{ font-size: 36px; font-weight: bold; letter-spacing: 10px; color: #0369a1; font-family: monospace; }}
            .footer {{ text-align: center; padding: 20px; font-size: 12px; color: #6b7280; }}
            .info {{ background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 12px; margin: 20px 0; font-size: 14px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin: 0; font-size: 24px;">Welcome to LitPulse!</h1>
                <p style="margin: 8px 0 0; opacity: 0.9;">Medical Literature, Curated for You</p>
            </div>
            <div class="content">
                <h2>Hello {user_name},</h2>
                <p>Thank you for signing up for LitPulse! To complete your registration and activate your 30-day Pro trial, please enter the verification code below:</p>
                <div class="code-box">
                    <span class="code">{code}</span>
                </div>
                <div class="info">
                    <strong>⏱️ This code expires in 15 minutes.</strong>
                </div>
                <p>Enter this code in the app to verify your email address and start exploring curated medical literature.</p>
                <p>If you didn't create an account with LitPulse, you can safely ignore this email.</p>
            </div>
            <div class="footer">
                <p>&copy; 2025 LitPulse. All rights reserved.</p>
                <p>Stay ahead in medical literature.</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Welcome to LitPulse!
    
    Hello {user_name},
    
    Thank you for signing up! Your verification code is:
    
    {code}
    
    This code expires in 15 minutes.
    
    Enter this code in the app to verify your email and activate your 30-day Pro trial.
    
    If you didn't create an account, ignore this email.
    
    - The LitPulse Team
    """
    
    return _send_email(to_email, subject, html_content, text_content, "SIGNUP_VERIFICATION")


def send_briefing_email(
    email: str,
    name: str,
    article_count: int,
    audio_ready: int,
    duration_min: int,
    digest_id: str,
) -> bool:
    """Send Daily Briefing notification email."""
    subject = f"Your {duration_min}-minute literature briefing is ready"

    briefing_url = f"{APP_BASE_URL}/digests/{digest_id}" if APP_BASE_URL else "#"

    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 520px; margin: 0 auto;">
        <h2 style="color: #1a56db;">Your Daily Briefing is Ready</h2>
        <p>Hi {name},</p>
        <p>Your latest literature digest has been processed into an audio briefing:</p>
        <div style="background: #f0f7ff; border-radius: 8px; padding: 16px; margin: 16px 0;">
            <p style="margin:0; font-size: 24px; font-weight: bold; color: #1a56db;">{duration_min} min</p>
            <p style="margin:4px 0 0; color: #555;">{audio_ready} audio takeaways from {article_count} articles</p>
        </div>
        <p><a href="{briefing_url}" style="display:inline-block; background:#1a56db; color:white; padding:10px 24px; border-radius:6px; text-decoration:none; font-weight:500;">Listen Now</a></p>
        <p style="font-size: 12px; color: #888; margin-top: 24px;">
            Generated from article abstracts. No patient data included.<br>
            LitPulse Pro &mdash; Your literature, your way.
        </p>
    </div>
    """

    text_content = f"""Hi {name},

Your {duration_min}-minute literature briefing is ready.
{audio_ready} audio takeaways from {article_count} articles.

Listen at: {briefing_url}

Generated from article abstracts. No patient data included.
- LitPulse Pro
"""

    return _send_email(email, subject, html_content, text_content, "DAILY_BRIEFING")

