from time import sleep
from datetime import datetime, time, timedelta
from dotenv import load_dotenv
import os
import logging
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from icalendar import Calendar
from apscheduler.jobstores.base import JobLookupError
from persistence import ensure_data_directory, save_user_reminders, load_user_reminders, load_all_users
from functools import wraps

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Dictionary to store user reminders
user_reminders = {}

# Get current working directory
work_dir = os.getcwd(); 

# Set up data directory
data_path = os.getenv("DATA_PATH", os.path.join(work_dir, 'data'))

# Load reminder timing configuration
reminder_hour = int(os.getenv('REMINDER_HOUR', '17'))  # Default 5 PM
reminder_minute = int(os.getenv('REMINDER_MINUTE', '0'))  # Default 0 minutes
reminder_interval_hours = int(os.getenv('REMINDER_INTERVAL_HOURS', '2'))  # Default 2 hours
logger.info(f"Configured reminder time: {reminder_hour}:{reminder_minute:02d}, interval: {reminder_interval_hours} hours")

# Parse whitelist users from environment
whitelist_users = []
whitelist_env = os.getenv('WHITELIST_USERS', '')
if whitelist_env:
    try:
        whitelist_users = [int(user_id.strip()) for user_id in whitelist_env.split(',') if user_id.strip()]
        logger.info(f"Loaded {len(whitelist_users)} whitelisted users")
    except ValueError as e:
        logger.error(f"Error parsing WHITELIST_USERS: {e}")

# Parse ignored terms from environment
ignored_terms = []
ignored_terms_env = os.getenv('IGNORED_TERMS', 'Wertstoffhof geschlossen')
if ignored_terms_env:
    ignored_terms = [term.strip() for term in ignored_terms_env.split('||') if term.strip()]
    logger.info(f"Loaded {len(ignored_terms)} ignored terms: {ignored_terms}")

def _now_like(reference: datetime) -> datetime:
    if isinstance(reference, datetime) and reference.tzinfo is not None:
        return datetime.now(tz=reference.tzinfo)
    return datetime.now()

def _event_cutoff(event_start: datetime) -> datetime:
    event_date = event_start.date()
    tzinfo = event_start.tzinfo
    # Reminder window ends at the day rollover into the event day (00:00).
    return datetime.combine(event_date, time.min, tzinfo=tzinfo) if tzinfo else datetime.combine(event_date, time.min)

def _is_event_expired(event_start: datetime, now: datetime | None = None) -> bool:
    now = now or _now_like(event_start)
    return now >= _event_cutoff(event_start)

def _safe_schedule_removal(job, *, user_id: int | None = None, event_id: str | None = None) -> None:
    if not job:
        return
    try:
        job.schedule_removal()
    except JobLookupError:
        logger.debug(f"Job already removed (user={user_id}, event={event_id})")
    except Exception as e:
        logger.warning(f"Could not remove job (user={user_id}, event={event_id}): {e}")

def _make_event_id(user_id: int, component, start_time: datetime, summary: str) -> str:
    uid = str(component.get('uid', '')).strip()
    base = f"{user_id}|{uid}|{start_time.isoformat()}|{summary}"
    digest = hashlib.md5(base.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{user_id}_{digest}"

def _format_event_when(event_start: datetime) -> str:
    if (
        event_start.hour == 0
        and event_start.minute == 0
        and event_start.second == 0
        and event_start.microsecond == 0
    ):
        return event_start.strftime("%Y-%m-%d")
    return event_start.strftime("%Y-%m-%d %H:%M")

def _start_time_sort_key(event_data: dict) -> float:
    when = event_data.get("start_time")
    if isinstance(when, datetime):
        try:
            return when.timestamp()
        except Exception:
            return float("inf")
    return float("inf")

def _extract_event_type(summary: str, categories) -> str:
    candidates: list[str] = []
    if categories:
        if isinstance(categories, (list, tuple, set)):
            candidates = [str(c).strip() for c in categories if str(c).strip()]
        elif hasattr(categories, "cats"):
            try:
                candidates = [str(c).strip() for c in categories.cats if str(c).strip()]
            except Exception:
                candidates = []
        if not candidates:
            candidates = [p.strip() for p in str(categories).split(",") if p.strip()]

    if candidates:
        return candidates[0]

    return summary.strip() or "Unknown"

def _prune_user_reminders(user_id: int, *, save: bool = False) -> int:
    if user_id not in user_reminders or not user_reminders[user_id]:
        return 0

    removed = 0
    for event_id in list(user_reminders[user_id].keys()):
        event_data = user_reminders[user_id].get(event_id)
        if not event_data:
            continue

        event_start = event_data.get("start_time")
        if not isinstance(event_start, datetime):
            continue

        now = _now_like(event_start)
        if event_data.get("acknowledged", False) or _is_event_expired(event_start, now):
            _safe_schedule_removal(event_data.get("job"), user_id=user_id, event_id=event_id)
            user_reminders[user_id].pop(event_id, None)
            removed += 1

    if removed and save:
        save_user_reminders(data_path, user_id, user_reminders[user_id])

    return removed

def whitelist_only(func):
    """Decorator to only allow whitelisted users to access the bot."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        
        # If whitelist is empty, allow all users (for backward compatibility)
        if not whitelist_users or user_id in whitelist_users:
            return await func(update, context, *args, **kwargs)
        
        # For non-whitelisted users, log and ignore silently
        logger.warning(f"Unauthorized access attempt by user {user_id}")
        # Don't send any response to non-whitelisted users
        return
    
    return wrapped

@whitelist_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        "Welcome to the Garbage Collection Reminder Bot!\n\n"
        "Upload an ICS calendar file to set up reminders for your garbage collection events.\n\n"
        "Use /help to see all available commands and how to use the bot."
    )

@whitelist_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "How to use this bot:\n\n"
        "1. Upload your ICS calendar file containing garbage collection events\n"
        "2. The bot will automatically set reminders for all events except 'Wertstoffhof geschlossen'\n"
        f"3. You'll receive reminders the day before at {reminder_hour}:{reminder_minute:02d}\n"
        f"4. Reminders will continue every {reminder_interval_hours} hours until 00:00 (start of the event day)\n"
        "5. Press 'Acknowledge' to stop reminders for a specific event\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/list - List all your upcoming reminders\n"
        "/clear - Clear all your reminders"
    )

@whitelist_only
async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all upcoming reminders for the user."""
    user_id = update.effective_user.id

    _prune_user_reminders(user_id, save=True)

    if user_id not in user_reminders or not user_reminders[user_id]:
        await update.message.reply_text("You don't have any active reminders.")
        return

    grouped: dict[str, list[tuple[str, dict]]] = {}
    for event_id, event_data in user_reminders[user_id].items():
        event_type = event_data.get("event_type") or event_data.get("summary") or "Unknown"
        grouped.setdefault(event_type, []).append((event_id, event_data))

    for event_type, items in grouped.items():
        grouped[event_type] = sorted(items, key=lambda item: _start_time_sort_key(item[1]))

    groups_sorted = sorted(grouped.items(), key=lambda item: item[0].casefold())

    lines: list[str] = ["Your upcoming reminders (sorted by type):", ""]
    for event_type, items in groups_sorted:
        next_when = items[0][1].get("start_time")
        next_when_str = _format_event_when(next_when) if isinstance(next_when, datetime) else "?"
        lines.append(f"{event_type} — next: {next_when_str} ({len(items)})")
        for _, event_data in items:
            when = event_data.get("start_time")
            if isinstance(when, datetime):
                lines.append(f"• {_format_event_when(when)}")
        lines.append("")

    await update.message.reply_text("\n".join(lines).rstrip())

@whitelist_only
async def clear_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all reminders for the user."""
    user_id = update.effective_user.id

    if user_id in user_reminders:
        # Cancel all scheduled jobs
        for event_id, event_data in user_reminders[user_id].items():
            _safe_schedule_removal(event_data.get("job"), user_id=user_id, event_id=event_id)

        # Clear the reminders
        user_reminders[user_id] = {}
        
        # Save the empty reminders to disk
        save_user_reminders(data_path, user_id, user_reminders[user_id])

        await update.message.reply_text("All your reminders have been cleared.")
    else:
        await update.message.reply_text("You don't have any active reminders.")

@whitelist_only
async def handle_ics_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uploaded ICS files."""
    user_id = update.effective_user.id

    if user_id not in user_reminders:
        user_reminders[user_id] = {}
    else:
        _prune_user_reminders(user_id, save=True)

    # Get the file
    file = await context.bot.get_file(update.message.document.file_id)

    # Download the file
    file_path = os.path.join(data_path, f"calendar_{user_id}.ics")
    logger.info(f"Downloading calendar file for user {user_id} to: {file_path}")
    try:
        downloaded_path = await file.download_to_drive(file_path)
        if not downloaded_path or not os.path.exists(downloaded_path):
            raise FileNotFoundError(f"Failed to download file to {file_path}")
        logger.info(f"File successfully downloaded to: {downloaded_path}")
    except Exception:
        logger.exception("Error downloading calendar file")
        await update.message.reply_text("Failed to download your calendar file. Please try again.")
        return

    try:
        # Parse the ICS file
        with open(file_path, 'rb') as f:
            cal_content = f.read()
            cal = Calendar.from_ical(cal_content)

        # Process events
        events_count = 0
        new_reminders: dict[str, dict] = {}
        
        for component in cal.walk():
            if component.name == "VEVENT":
                summary = str(component.get('summary', 'No Title'))
                categories_value = component.get('categories', '')
                categories = str(categories_value or '')

                # Skip events based on ignored terms
                should_ignore = False
                for term in ignored_terms:
                    if term in summary or term in categories:
                        should_ignore = True
                        logger.info(f"Ignoring event matching term '{term}': {summary}")
                        break
                
                if should_ignore:
                    continue  # Skip this event and continue with the next one

                event_type = _extract_event_type(summary, categories_value)

                dtstart = component.get('dtstart')
                if not dtstart:
                    logger.warning(f"Skipping event without dtstart: {summary}")
                    continue

                start_time = dtstart.dt

                # Convert to datetime if it's a date
                if not isinstance(start_time, datetime):
                    start_time = datetime.combine(start_time, time.min)

                now = _now_like(start_time)
                if _is_event_expired(start_time, now):
                    logger.info(f"Skipping expired event: {summary} at {start_time.isoformat()}")
                    continue

                event_id = _make_event_id(user_id, component, start_time, summary)

                # Schedule the first reminder (day before at configured time)
                reminder_time = start_time.replace(hour=reminder_hour, minute=reminder_minute, second=0, microsecond=0) - timedelta(days=1)
                next_reminder_time = reminder_time if reminder_time > now else now + timedelta(seconds=5)

                new_reminders[event_id] = {
                    'summary': summary,
                    'event_type': event_type,
                    'start_time': start_time,
                    'acknowledged': False,
                    'job': None,
                    'next_reminder_time': next_reminder_time,
                    'first_reminder': next_reminder_time.date() < start_time.date(),
                }
                events_count += 1

        # Replace existing reminders only after successfully parsing the calendar
        logger.info(f"Replacing {len(user_reminders[user_id])} existing reminders for user {user_id} with {events_count} reminders from uploaded calendar")
        for event_id, event_data in user_reminders[user_id].items():
            _safe_schedule_removal(event_data.get("job"), user_id=user_id, event_id=event_id)

        user_reminders[user_id] = new_reminders

        # Schedule reminder jobs
        for event_id, event_data in user_reminders[user_id].items():
            event_start = event_data["start_time"]
            now = _now_like(event_start)
            next_time = event_data.get("next_reminder_time")
            if not isinstance(next_time, datetime) or next_time <= now:
                next_time = now + timedelta(seconds=5)
                event_data["next_reminder_time"] = next_time

            delay = (next_time - now).total_seconds()
            job = context.job_queue.run_once(
                send_reminder,
                delay,
                data={'user_id': user_id, 'event_id': event_id},
                name=f"reminder_{event_id}"
            )
            event_data["job"] = job

        # Save reminders to disk - contains only non-expired, non-ignored events
        save_user_reminders(data_path, user_id, user_reminders[user_id])
        
        await update.message.reply_text(f"Calendar processed successfully! Set up {events_count} reminders.")

    except Exception as e:
        logger.exception("Error processing ICS file")
        await update.message.reply_text(f"Error processing your calendar file: {str(e)}")

    finally:
        # Clean up the file
        if os.path.exists(file_path):
            os.remove(file_path)

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a reminder to the user."""
    job = context.job
    user_id = job.data['user_id']
    event_id = job.data['event_id']

    # Check if the event exists and is not acknowledged
    if (user_id in user_reminders and
            event_id in user_reminders[user_id] and
            not user_reminders[user_id][event_id]['acknowledged']):

        event_data = user_reminders[user_id][event_id]
        event_summary = event_data['summary']
        event_start = event_data['start_time']
        now = _now_like(event_start)

        if _is_event_expired(event_start, now):
            user_reminders[user_id].pop(event_id, None)
            save_user_reminders(data_path, user_id, user_reminders[user_id])
            return

        event_date_str = event_start.strftime('%Y-%m-%d')

        # Create the acknowledge button
        keyboard = [[InlineKeyboardButton("Acknowledge", callback_data=f"ack_{event_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        days_until = (event_start.date() - now.date()).days
        if days_until == 1:
            message = f"⚠️ REMINDER: You have '{event_summary}' scheduled for tomorrow ({event_date_str})."
        elif days_until == 0:
            message = f"⚠️ REMINDER: Don't forget about '{event_summary}' scheduled for today ({event_date_str})."
        else:
            message = f"⚠️ REMINDER: Don't forget about '{event_summary}' coming up on {event_date_str}."

        # Send the reminder
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=reply_markup
        )

        # Schedule the next reminder if needed
        cutoff = _event_cutoff(event_start)
        next_reminder_time = now + timedelta(hours=reminder_interval_hours)

        if next_reminder_time < cutoff:
            next_job = context.job_queue.run_once(
                send_reminder,
                reminder_interval_hours * 3600,  # Convert hours to seconds
                data={'user_id': user_id, 'event_id': event_id},
                name=f"reminder_{event_id}"
            )
            user_reminders[user_id][event_id]['job'] = next_job
            user_reminders[user_id][event_id]['next_reminder_time'] = next_reminder_time
            user_reminders[user_id][event_id]['first_reminder'] = next_reminder_time.date() < event_start.date()
            
            # Save updated reminder info
            save_user_reminders(data_path, user_id, user_reminders[user_id])
        else:
            user_reminders[user_id][event_id]['job'] = None
            # Keep a sentinel time so restarts don't schedule extra reminders before expiry.
            user_reminders[user_id][event_id]['next_reminder_time'] = cutoff
            user_reminders[user_id][event_id]['first_reminder'] = False
            save_user_reminders(data_path, user_id, user_reminders[user_id])

@whitelist_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()

    # Extract the event_id from the callback data
    if query.data.startswith("ack_"):
        event_id = query.data[4:]
        user_id = update.effective_user.id

        if user_id in user_reminders and event_id in user_reminders[user_id]:
            event_summary = user_reminders[user_id][event_id]['summary']

            # Cancel any scheduled jobs for this event, then remove it from the reminder list
            _safe_schedule_removal(user_reminders[user_id][event_id].get("job"), user_id=user_id, event_id=event_id)
            user_reminders[user_id].pop(event_id, None)
            save_user_reminders(data_path, user_id, user_reminders[user_id])

            await query.edit_message_text(
                f"✅ Acknowledged: '{event_summary}'. No more reminders will be sent for this event.")

async def restore_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore jobs from saved data when the bot starts."""
    user_ids = load_all_users(data_path)
    
    jobs_restored = 0
    expired_events_removed = 0
    
    for user_id in user_ids:
        loaded_reminders = load_user_reminders(data_path, user_id)
        user_reminders[user_id] = {}  # Start with empty dict to ensure we only store valid events
        user_changed = False
        
        for event_id, event_data in loaded_reminders.items():
            # Skip acknowledged events and expired events
            if event_data.get('acknowledged', False):
                user_changed = True
                continue
                
            event_start = event_data.get('start_time')
            if not isinstance(event_start, datetime):
                user_changed = True
                continue

            now = _now_like(event_start)
            if _is_event_expired(event_start, now):
                expired_events_removed += 1
                user_changed = True
                continue
                
            # Add this valid event to the user's reminders
            user_reminders[user_id][event_id] = event_data
            if "event_type" not in user_reminders[user_id][event_id]:
                user_reminders[user_id][event_id]["event_type"] = user_reminders[user_id][event_id].get("summary") or "Unknown"
                user_changed = True
                
            # Determine next reminder time
            next_reminder_time = event_data.get('next_reminder_time')
            
            # If no next_reminder_time or it's in the past, calculate a new one
            if not isinstance(next_reminder_time, datetime) or next_reminder_time <= now:
                day_before = event_start.replace(hour=reminder_hour, minute=reminder_minute, second=0, microsecond=0) - timedelta(days=1)
                next_reminder_time = day_before if day_before > now else now + timedelta(seconds=5)
                user_changed = True
            
            # Schedule the job if we have a valid next_reminder_time
            cutoff = _event_cutoff(event_start)
            if isinstance(next_reminder_time, datetime) and next_reminder_time > now and next_reminder_time < cutoff:
                delay = (next_reminder_time - now).total_seconds()
                job = context.job_queue.run_once(
                    send_reminder,
                    delay,
                    data={'user_id': user_id, 'event_id': event_id},
                    name=f"reminder_{event_id}"
                )
                user_reminders[user_id][event_id]['job'] = job
                user_reminders[user_id][event_id]['next_reminder_time'] = next_reminder_time
                user_reminders[user_id][event_id]['first_reminder'] = next_reminder_time.date() < event_start.date()
                jobs_restored += 1
            else:
                user_reminders[user_id][event_id]['job'] = None
                user_reminders[user_id][event_id]['next_reminder_time'] = cutoff
                user_reminders[user_id][event_id]['first_reminder'] = False
                user_changed = True
        
        # Save back the filtered reminders
        if user_changed:
            save_user_reminders(data_path, user_id, user_reminders[user_id])
    
    logger.info(f"Restored {jobs_restored} reminder jobs for {len(user_ids)} users")
    if expired_events_removed > 0:
        logger.info(f"Removed {expired_events_removed} expired events during startup")

async def setup_bot_commands(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register bot commands for the Telegram interface."""
    commands = [
        BotCommand("start", "Start the bot and show welcome message"),
        BotCommand("help", "Show help and usage instructions"),
        BotCommand("list", "List all your upcoming reminders"),
        BotCommand("clear", "Clear all your reminders")
    ]
    await context.bot.set_my_commands(commands)
    logger.info("Bot commands registered.")

def main() -> None:
    """Start the bot."""
    ensure_data_directory(data_path)

    # Get token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    # Create the Application
    application = Application.builder().token(token).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(CommandHandler("clear", clear_reminders))
    ics_filter = filters.Document.MimeType("text/calendar")
    if hasattr(filters.Document, "FileExtension"):
        ics_filter = ics_filter | filters.Document.FileExtension("ics")
    application.add_handler(MessageHandler(ics_filter, handle_ics_file))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Set up jobs restoration and bot commands when the bot starts
    application.job_queue.run_once(restore_jobs, 1)
    application.job_queue.run_once(setup_bot_commands, 2)

    # Run the bot
    while True:
        try:
            logger.info("Starting the bot...")
            application.run_polling()
        except Exception:
            logger.exception("Bot crashed with error")
            logger.info("Attempting restart in 20 seconds...")
            sleep(20)


if __name__ == '__main__':
    main()
