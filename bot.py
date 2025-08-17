import os
import re
import asyncio
import aiohttp
import discord
from discord.ext import commands
import imaplib
import email
from email.header import decode_header
import logging
from typing import Optional, Tuple, List
from contextlib import asynccontextmanager
import time
from datetime import datetime, timezone

# Configure logging with debug level and detailed formatting
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot_debug.log")],
)
logger = logging.getLogger(__name__)

# Bot configuration
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
TOKEN = os.getenv("TOKEN")
EMAIL = str(os.getenv("EMAIL"))
PASSWORD = str(os.getenv("PASSWORD"))
SERVER = "imap.gmail.com"


# Rate limiting
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 5
user_request_times = {}

VERIFY_LINK_REGEX = re.compile(
    r"\[(https://www\.netflix\.com/account/travel/verify[^\]]*)\]"
)

# Regex pattern for Netflix sign-in codes (typically 6-8 digit codes)
SIGN_IN_CODE_REGEX = re.compile(
    r"(?:nhập mã này để đăng nhập|mã đăng nhập|sign.?in code|verification code)[^\d\n]*(\d{4,8})",
    re.IGNORECASE | re.MULTILINE,
)


def parse_email_date(date_str: str) -> Optional[datetime]:
    """Parse email date string to datetime object"""
    try:
        if not date_str:
            return None

        # Common email date formats
        from email.utils import parsedate_to_datetime

        parsed_date = parsedate_to_datetime(date_str)
        return parsed_date
    except Exception as e:
        logger.error(f"Error parsing email date '{date_str}': {e}")
        return None


def is_code_expired(
    email_date: Optional[datetime], expiry_minutes: int = 15
) -> Tuple[bool, str]:
    """Check if sign-in code is expired based on email timestamp"""
    try:
        if not email_date:
            return True, "Unknown email date"

        now = datetime.now(timezone.utc)
        # Ensure email_date is timezone aware
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=timezone.utc)

        time_diff = now - email_date
        minutes_elapsed = time_diff.total_seconds() / 60

        if minutes_elapsed > expiry_minutes:
            return (
                True,
                f"Code expired ({minutes_elapsed:.1f} minutes old, limit: {expiry_minutes} minutes)",
            )
        else:
            remaining_minutes = expiry_minutes - minutes_elapsed
            return False, f"Code valid (expires in {remaining_minutes:.1f} minutes)"

    except Exception as e:
        logger.error(f"Error checking code expiry: {e}")
        return True, f"Error checking expiry: {e}"


def decode_email_subject(subject: str) -> str:
    """Decode MIME encoded email subject"""
    try:
        decoded_parts = decode_header(subject)
        decoded_subject = ""

        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                if encoding:
                    decoded_subject += part.decode(encoding)
                else:
                    decoded_subject += part.decode("utf-8", errors="ignore")
            else:
                decoded_subject += part

        return decoded_subject
    except Exception as e:
        logger.error(f"Error decoding subject '{subject}': {e}")
        return subject  # Return original if decoding fails


def is_rate_limited(user_id: int) -> bool:
    """Check if user is rate limited"""
    current_time = time.time()

    if user_id not in user_request_times:
        user_request_times[user_id] = []

    # Remove old requests outside the window
    old_count = len(user_request_times[user_id])
    user_request_times[user_id] = [
        req_time
        for req_time in user_request_times[user_id]
        if current_time - req_time < RATE_LIMIT_WINDOW
    ]
    new_count = len(user_request_times[user_id])

    if len(user_request_times[user_id]) >= RATE_LIMIT_MAX_REQUESTS:
        logger.warning(
            f"User {user_id} is rate limited - {len(user_request_times[user_id])} requests in window"
        )
        return True

    user_request_times[user_id].append(current_time)
    return False


@asynccontextmanager
async def get_imap_connection():
    """Context manager for IMAP connection with proper cleanup"""
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(SERVER)

        mail.login(EMAIL, PASSWORD)
        logger.info("IMAP login successful")

        yield mail
    except Exception as e:
        logger.error(f"IMAP connection error: {e}")
        raise
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except Exception as e:
                logger.error(f"Error closing IMAP connection: {e}")


async def get_netflix_emails(subject: str) -> Optional[Tuple[str, str]]:
    """Get Netflix emails from IMAP server"""
    try:
        async with get_imap_connection() as mail:
            mail.select("Inbox")

            # Handle Unicode characters in search criteria by using charset
            search_criteria = f'(FROM "info@account.netflix.com" SUBJECT "{subject}")'

            try:
                # First try with UTF-8 encoding for Unicode subjects
                status, messages = mail.search("UTF-8", search_criteria)
            except Exception as e:
                # Fallback to searching all Netflix emails and filter later
                broad_search_criteria = '(FROM "info@account.netflix.com")'
                status, messages = mail.search(None, broad_search_criteria)

            if status != "OK" or not messages[0]:
                logger.info("No Netflix emails found")
                return None

            mail_ids = messages[0].split()

            # Search through emails to find one with matching subject
            for mail_id in reversed(mail_ids):  # Start with most recent
                try:
                    status, message_data = mail.fetch(mail_id, "(RFC822)")
                    if status != "OK":
                        continue

                    msg = email.message_from_bytes(message_data[0][1])
                    raw_subject = msg.get("subject", "")
                    email_subject = decode_email_subject(raw_subject)

                    # Check if subject matches (case-insensitive, partial match)
                    if subject.lower() in email_subject.lower():

                        content = _extract_email_content(msg)
                        if content:

                            # Get email timestamp for expiry checking
                            email_date = msg.get("Date", "")

                            return content, email_date
                        else:
                            return content, None
                except Exception as e:
                    continue

            logger.warning(f"No emails found with subject containing: {subject}")
            return None

    except Exception as e:
        logger.error(f"Error in get_netflix_emails: {e}")
        return None


# DEBUG FUNCTIONS - Development only
async def get_recent_email_subjects(count: int = 5) -> List[str]:
    """Get the subjects of recent Netflix emails for debugging"""
    try:
        async with get_imap_connection() as mail:
            mail.select("Inbox")

            # Search for all Netflix emails
            search_criteria = '(FROM "info@account.netflix.com")'

            status, messages = mail.search(None, search_criteria)

            if status != "OK" or not messages[0]:
                logger.info("No Netflix emails found")
                return []

            mail_ids = messages[0].split()
            if not mail_ids:
                return []

            # Get the last N emails
            recent_ids = mail_ids[-count:] if len(mail_ids) >= count else mail_ids
            subjects = []

            for mail_id in reversed(recent_ids):  # Most recent first
                try:
                    status, message_data = mail.fetch(mail_id, "(RFC822)")
                    if status != "OK":
                        continue

                    msg = email.message_from_bytes(message_data[0][1])
                    raw_subject = msg.get("subject", "No subject")
                    decoded_subject = decode_email_subject(raw_subject)
                    subjects.append(decoded_subject)
                except Exception as e:
                    continue

            logger.info(f"Found {len(subjects)} recent email subjects")
            return subjects

    except Exception as e:
        logger.error(f"Error getting recent email subjects: {e}")
        return []


async def get_latest_email_subject() -> Optional[str]:
    """Get the subject of the latest Netflix email for debugging"""
    try:
        async with get_imap_connection() as mail:
            mail.select("Inbox")

            # Search for all Netflix emails
            search_criteria = '(FROM "info@account.netflix.com")'

            status, messages = mail.search(None, search_criteria)

            if status != "OK" or not messages[0]:
                logger.info("No Netflix emails found")
                return None

            mail_ids = messages[0].split()
            if not mail_ids:
                return None

            # Get the latest email (last ID)
            latest_mail_id = mail_ids[-1]

            status, message_data = mail.fetch(latest_mail_id, "(RFC822)")
            if status != "OK":
                logger.error("Error fetching latest email")
                return None

            msg = email.message_from_bytes(message_data[0][1])
            raw_subject = msg.get("subject", "No subject")
            decoded_subject = decode_email_subject(raw_subject)

            logger.info(f"Latest Netflix email subject (decoded): {decoded_subject}")
            print(f"DEBUG - Latest Netflix email subject (decoded): {decoded_subject}")

            return decoded_subject

    except Exception as e:
        logger.error(f"Error getting latest email subject: {e}")
        return None


async def get_sign_in_code() -> Optional[Tuple[str, bool, str]]:
    """Get sign in code from Netflix email with expiry check
    Returns: (code, is_expired, expiry_message) or None
    """
    try:
        result = await get_netflix_emails("Mã đăng nhập")

        if not result:
            logger.warning("No email content found for sign in code")
            return None

        content, email_date_str = result
        print(
            f"DEBUG - Email content: {content[:500]}..."
        )  # DEBUG: Print first 500 chars for debugging

        # Parse email date for expiry checking
        email_date = parse_email_date(email_date_str) if email_date_str else None

        found_code = None
        pattern_used = ""

        match = SIGN_IN_CODE_REGEX.search(content)
        if match:
            found_code = match.group(1)
            pattern_used = "main regex"
            logger.info(f"Sign in code found: {found_code}")
        else:
            logger.warning("No sign in code found in email content")

            # Try alternative patterns for Vietnamese format
            # Look for standalone 4-8 digit numbers after Vietnamese login text
            vietnamese_pattern = re.search(
                r"nhập mã này để đăng nhập[\s\n]+nhập mã này để đăng nhập[\s\n]+(\d{4,8})",
                content,
                re.IGNORECASE | re.MULTILINE,
            )
            if vietnamese_pattern:
                found_code = vietnamese_pattern.group(1)
                pattern_used = "Vietnamese pattern"
                logger.info(f"Vietnamese pattern found code: {found_code}")
            else:
                # Try simple standalone number pattern
                simple_code_match = re.search(
                    r"^\s*(\d{4,8})\s*$", content, re.MULTILINE
                )
                if simple_code_match:
                    found_code = simple_code_match.group(1)
                    pattern_used = "simple pattern"
                    logger.info(f"Simple pattern found code: {found_code}")
                else:
                    # Last resort: any 4-8 digit number
                    fallback_match = re.search(r"\b(\d{4,8})\b", content)
                    if fallback_match:
                        found_code = fallback_match.group(1)
                        pattern_used = "fallback pattern"
                        logger.info(f"Fallback pattern found code: {found_code}")

        if found_code:
            # Check expiry
            is_expired, expiry_msg = is_code_expired(email_date)
            logger.info(f"Code {found_code} expiry check: {expiry_msg}")
            return found_code, is_expired, expiry_msg
        else:
            return None

    except Exception as e:
        logger.error(f"Error in get_sign_in_code: {e}")
        return None


async def get_verify_link() -> Optional[str]:
    """Get verification link from Netflix email asynchronously"""
    try:
        result = await _get_verify_link_async("temporary access code")
        return result
    except Exception as e:
        logger.error(f"Error getting verify link: {e}")
        return None


async def _get_verify_link_async(subject: str) -> Optional[str]:
    """Async version of get_verify_link"""
    try:
        content = await get_netflix_emails(subject)

        if not content:
            logger.warning("No email content found for verification link")
            return None

        match = VERIFY_LINK_REGEX.search(content)
        if match:
            link = match.group(1)
            logger.info(f"Verification link found: {link[:50]}...")
            return link
        else:
            logger.warning("No verification link found in email content")
            return None

    except Exception as e:
        logger.error(f"Error in _get_verify_link_sync: {e}")
        return None


def _extract_email_content(msg) -> Optional[str]:
    """Extract text content from email message"""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    content = part.get_payload(decode=True).decode(
                        "utf-8", errors="replace"
                    )
                    if content:
                        return content
        else:
            content = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            if content:
                return content

        logger.warning("No text content found in email")
        return None
    except Exception as e:
        logger.error(f"Error extracting email content: {e}")
        return None


async def access_verify_link() -> Optional[str]:
    """Access verification link and extract challenge code asynchronously"""
    try:
        link = await get_verify_link()
        if not link:
            logger.warning("No verification link available")
            return None

        # Use aiohttp for async HTTP requests
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(link) as response:

                if response.status == 200:
                    html_content = await response.text()

                    challenge_code = _extract_challenge_code(html_content)
                    if challenge_code:
                        logger.info(f"Challenge code extracted: {challenge_code}")
                    else:
                        logger.warning("No challenge code found in HTML")
                    return challenge_code
                else:
                    logger.error(
                        f"Failed to access verification link, status: {response.status}"
                    )
                    return None

    except asyncio.TimeoutError:
        logger.error("Timeout accessing verification link")
        return None
    except Exception as e:
        logger.error(f"Error accessing verification link: {e}")
        return None


def _extract_challenge_code(html_content: str) -> Optional[str]:
    """Extract challenge code from HTML content"""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_content, "html.parser")

        challenge_code_div = soup.find("div", class_="challenge-code")

        if challenge_code_div:
            code = challenge_code_div.get_text(strip=True)
            if code:
                logger.info(f"Successfully extracted challenge code: {code}")
                return code
            else:
                logger.warning("Challenge code div is empty")
        else:
            logger.warning("No challenge code div found in HTML")
        logger.warning("No challenge code found in HTML content")
        return None

    except Exception as e:
        logger.error(f"Error extracting challenge code: {e}")
        return None


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    logger.info(f"Bot is ready to serve {len(bot.guilds)} guilds")


@bot.command(name="hello")
async def hello(ctx):
    """Simple hello command to verify bot is active"""
    user_id = ctx.author.id
    user_name = ctx.author.name
    channel_id = ctx.channel.id

    try:
        await ctx.send("Hello! How can I assist you today?")
    except Exception as e:
        logger.error(f"Error in hello command: {e}")


@bot.command(name="signin")
async def signin(ctx):
    """Get Netflix sign-in code"""
    user_id = ctx.author.id
    user_name = ctx.author.name
    channel_id = ctx.channel.id

    logger.info(
        f"Sign-in command triggered by user {user_id} ({user_name}) in channel {channel_id}"
    )

    try:
        # Check rate limiting
        if is_rate_limited(user_id):
            logger.warning(f"User {user_id} ({user_name}) hit rate limit")
            await ctx.send("⚠️ Rate limit exceeded. Please wait before trying again.")
            return

        await ctx.send("🔍 Searching for Netflix sign-in code email...")

        result = await get_sign_in_code()

        if result:
            code, is_expired, expiry_msg = result
            logger.info(
                f"Successfully retrieved sign-in code for user {user_id}: {code} - {expiry_msg}"
            )

            if is_expired:
                await ctx.send(f"⚠️ Sign-in code: **{code}** (EXPIRED)\n❌ {expiry_msg}")
            else:
                await ctx.send(f"✅ Sign-in code: **{code}**\n⏰ {expiry_msg}")
        else:
            logger.warning(f"Failed to retrieve sign-in code for user {user_id}")
            await ctx.send(
                "❌ Failed to get the sign-in code. Please ensure you've requested a sign-in code from Netflix first."
            )

    except Exception as e:
        logger.error(f"Error in signin command for user {user_id}: {e}")
        await ctx.send("❌ An error occurred while processing your request.")


@bot.command(name="verify")
async def verify(ctx):
    """Get Netflix verification PIN code"""
    user_id = ctx.author.id
    user_name = ctx.author.name
    channel_id = ctx.channel.id

    logger.info(
        f"Verify command triggered by user {user_id} ({user_name}) in channel {channel_id}"
    )

    try:
        if is_rate_limited(user_id):
            logger.warning(f"User {user_id} ({user_name}) hit rate limit")
            await ctx.send("⚠️ Rate limit exceeded. Please wait before trying again.")
            return

        await ctx.send("🔍 Searching for Netflix verification email...")

        challenge_code = await access_verify_link()

        if challenge_code:
            logger.info(
                f"Successfully retrieved challenge code for user {user_id}: {challenge_code}"
            )
            await ctx.send(f"✅ Challenge code: **{challenge_code}**")
        else:
            logger.warning(f"Failed to retrieve challenge code for user {user_id}")
            await ctx.send(
                "❌ Failed to get the challenge code. Please ensure you've requested a PIN from Netflix first."
            )

    except Exception as e:
        logger.error(f"Error in verify command for user {user_id}: {e}")
        await ctx.send("❌ An error occurred while processing your request.")


@bot.event
async def on_command_error(ctx, error):
    """Global error handler for commands"""
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands

    logger.error(f"Command error in {ctx.command} for user {ctx.author.id}: {error}")
    await ctx.send("❌ An error occurred while processing your command.")


if __name__ == "__main__":
    logger.info("Starting Netflix Receive PIN Discord Bot")
    # get_sign_in_code()  # Commented out as it's not needed for bot startup

    if not TOKEN:
        logger.error(
            "Discord bot token not found. Please set the TOKEN environment variable."
        )
        exit(1)

    if not EMAIL or not PASSWORD:
        logger.error(
            "Email credentials not found. Please set EMAIL and PASSWORD environment variables."
        )
        exit(1)

    logger.info("All environment variables validated")

    try:
        logger.info("Attempting to start Discord bot")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        exit(1)
