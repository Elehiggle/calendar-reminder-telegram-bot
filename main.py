from time import sleep
from datetime import datetime, time, timedelta
from dotenv import load_dotenv
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from icalendar import Calendar
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
        "2. The bot will automatically set reminders for all events except 'Werstoffhof geschlossen'\n"
        f"3. You'll receive reminders the day before at {reminder_hour}:{reminder_minute:02d}\n"
        f"4. Reminders will continue every {reminder_interval_hours} hours until midnight on the event day\n"
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

    if user_id not in user_reminders or not user_reminders[user_id]:
        await update.message.reply_text("You don't have any active reminders.")
        return

    reminders_text = "Your upcoming reminders:\n\n"
    for event_id, event_data in user_reminders[user_id].items():
        reminders_text += f"• {event_data['summary']} on {event_data['start_time'].strftime('%Y-%m-%d %H:%M')}\n"

    await update.message.reply_text(reminders_text)

@whitelist_only
async def clear_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all reminders for the user."""
    user_id = update.effective_user.id

    if user_id in user_reminders:
        # Cancel all scheduled jobs
        for event_id, event_data in user_reminders[user_id].items():
            if 'job' in event_data and event_data['job']:
                event_data['job'].schedule_removal()

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

    # Initialize user's reminders dictionary if it doesn't exist
    if user_id not in user_reminders:
        user_reminders[user_id] = {}
    else:
        # Cancel all existing scheduled jobs before replacing them
        for event_id, event_data in user_reminders[user_id].items():
            if 'job' in event_data and event_data['job']:
                event_data['job'].schedule_removal()
        
        # Clear existing reminders when processing a new calendar file
        user_reminders[user_id] = {}

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
    except Exception as e:
        logger.error(f"Error downloading calendar file: {e}")
        await update.message.reply_text("Failed to download your calendar file. Please try again.")
        return

    try:
        # Parse the ICS file
        with open(file_path, 'rb') as f:
            cal_content = f.read()
            cal = Calendar.from_ical(cal_content.decode('utf-8'))

        # Process events
        events_count = 0
        now = datetime.now()
        
        for component in cal.walk():
            if component.name == "VEVENT":
                summary = str(component.get('summary', 'No Title'))
                categories = str(component.get('categories', ''))

                # Skip events based on ignored terms
                should_ignore = False
                for term in ignored_terms:
                    if term in summary or term in categories:
                        should_ignore = True
                        logger.info(f"Ignoring event matching term '{term}': {summary}")
                        break
                
                if should_ignore:
                    continue  # Skip this event and continue with the next one

                start_time = component.get('dtstart').dt

                # Convert to datetime if it's a date
                if not isinstance(start_time, datetime):  # It's a date
                    start_time = datetime.combine(start_time, datetime.min.time())

                # Skip events that have already passed
                if start_time < now:
                    logger.info(f"Skipping past event: {summary} at {start_time.isoformat()}")
                    continue

                event_id = f"{user_id}_{summary}_{start_time.isoformat()}"

                # Store event information - only reached if event is not ignored and not expired
                user_reminders[user_id][event_id] = {
                    'summary': summary,
                    'start_time': start_time,
                    'acknowledged': False,
                    'job': None,
                    'first_reminder': True
                }

                # Schedule the first reminder (day before at configured time)
                reminder_time = start_time.replace(hour=reminder_hour, minute=reminder_minute, second=0) - timedelta(days=1)

                # Use standard delay if reminder time is in future, otherwise use a small delay.
                # In case the very first bot run is after the configured time, we should still send the reminder when there is an event soon
                delay = max((reminder_time - now).total_seconds(), 0) if reminder_time > now else 5

                job = context.job_queue.run_once(
                    send_reminder,
                    delay,
                    data={'user_id': user_id, 'event_id': event_id, 'first_reminder': True},
                    name=f"reminder_{event_id}"
                )
                user_reminders[user_id][event_id]['job'] = job
                user_reminders[user_id][event_id]['next_reminder_time'] = reminder_time
                events_count += 1

        # Save reminders to disk - now contains only future, non-ignored events
        save_user_reminders(data_path, user_id, user_reminders[user_id])
        
        await update.message.reply_text(f"Calendar processed successfully! Set up {events_count} reminders.")

    except Exception as e:
        logger.error(f"Error processing ICS file: {e}")
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
    first_reminder = job.data.get('first_reminder', False)

    # Check if the event exists and is not acknowledged
    if (user_id in user_reminders and
            event_id in user_reminders[user_id] and
            not user_reminders[user_id][event_id]['acknowledged']):

        event_data = user_reminders[user_id][event_id]
        event_summary = event_data['summary']
        event_date = event_data['start_time'].strftime('%Y-%m-%d')

        # Create the acknowledge button
        keyboard = [[InlineKeyboardButton("Acknowledge", callback_data=f"ack_{event_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if first_reminder:
            message = f"⚠️ REMINDER: You have '{event_summary}' scheduled for tomorrow ({event_date})."
        else:
            message = f"⚠️ REMINDER: Don't forget about '{event_summary}' scheduled for today ({event_date})."

        # Send the reminder
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=reply_markup
        )

        # Schedule the next reminder if needed
        event_start = event_data['start_time']
        midnight = event_start.replace(hour=0, minute=0, second=0)
        now = datetime.now()

        # If we haven't reached midnight of the event day, schedule another reminder at the configured interval
        if now + timedelta(hours=reminder_interval_hours) < midnight:
            next_reminder_time = now + timedelta(hours=reminder_interval_hours)
            next_job = context.job_queue.run_once(
                send_reminder,
                reminder_interval_hours * 3600,  # Convert hours to seconds
                data={'user_id': user_id, 'event_id': event_id, 'first_reminder': False},
                name=f"reminder_{event_id}"
            )
            user_reminders[user_id][event_id]['job'] = next_job
            user_reminders[user_id][event_id]['next_reminder_time'] = next_reminder_time
            user_reminders[user_id][event_id]['first_reminder'] = False
            
            # Save updated reminder info
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
            # Mark the event as acknowledged
            user_reminders[user_id][event_id]['acknowledged'] = True

            # Cancel any scheduled jobs for this event
            if user_reminders[user_id][event_id]['job']:
                try:
                    user_reminders[user_id][event_id]['job'].schedule_removal()
                except Exception as e:
                    logger.warning(f"Could not remove job for event {event_id}: {e}")
                # Clear the job reference regardless
                user_reminders[user_id][event_id]['job'] = None

            # Save the updated status
            save_user_reminders(data_path, user_id, user_reminders[user_id])
            
            event_summary = user_reminders[user_id][event_id]['summary']
            await query.edit_message_text(
                f"✅ Acknowledged: '{event_summary}'. No more reminders will be sent for this event.")

async def restore_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore jobs from saved data when the bot starts."""
    user_ids = load_all_users(data_path)
    
    now = datetime.now()
    jobs_restored = 0
    expired_events_removed = 0
    
    for user_id in user_ids:
        loaded_reminders = load_user_reminders(data_path, user_id)
        user_reminders[user_id] = {}  # Start with empty dict to ensure we only store valid events
        
        for event_id, event_data in loaded_reminders.items():
            # Skip acknowledged events and expired events
            if event_data.get('acknowledged', False):
                continue
                
            event_start = event_data['start_time']
            # Skip events that have already passed
            if event_start < now:
                expired_events_removed += 1
                continue
                
            # Add this valid event to the user's reminders
            user_reminders[user_id][event_id] = event_data
                
            # Determine next reminder time
            next_reminder_time = event_data.get('next_reminder_time')
            first_reminder = event_data.get('first_reminder', True)
            
            # If no next_reminder_time or it's in the past, calculate a new one
            if not next_reminder_time or (isinstance(next_reminder_time, datetime) and next_reminder_time < now):
                # Calculate when the day-before reminder would be (at configured time)
                day_before = event_start.replace(hour=reminder_hour, minute=reminder_minute, second=0) - timedelta(days=1)
                
                if day_before > now:
                    # We can still do the day-before reminder
                    next_reminder_time = day_before
                    first_reminder = True
                else:
                    # Event is today, schedule reminder for the configured interval from now or at event time
                    next_reminder_time = now + timedelta(hours=reminder_interval_hours)
                    if next_reminder_time > event_start:
                        next_reminder_time = event_start - timedelta(minutes=15)  # 15 min before event
                    first_reminder = False
            
            # Schedule the job if we have a valid next_reminder_time
            if isinstance(next_reminder_time, datetime) and next_reminder_time > now:
                delay = (next_reminder_time - now).total_seconds()
                job = context.job_queue.run_once(
                    send_reminder,
                    delay,
                    data={'user_id': user_id, 'event_id': event_id, 'first_reminder': first_reminder},
                    name=f"reminder_{event_id}"
                )
                user_reminders[user_id][event_id]['job'] = job
                user_reminders[user_id][event_id]['next_reminder_time'] = next_reminder_time
                user_reminders[user_id][event_id]['first_reminder'] = first_reminder
                jobs_restored += 1
        
        # Save back the filtered reminders
        if expired_events_removed > 0:
            save_user_reminders(data_path, user_id, user_reminders[user_id])
    
    logger.info(f"Restored {jobs_restored} reminder jobs for {len(user_ids)} users")
    if expired_events_removed > 0:
        logger.info(f"Removed {expired_events_removed} expired events during startup")

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
    application.add_handler(MessageHandler(filters.Document.MimeType("text/calendar"), handle_ics_file))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Set up jobs restoration when the bot starts
    application.job_queue.run_once(restore_jobs, 1)

    # Run the bot
    while True:
        try:
            logger.info("Starting the bot...")
            application.run_polling()
        except Exception as e:
            logger.error(f"Bot crashed with error: {e}")
            logger.info("Attempting restart in 20 seconds...")
            sleep(20)


if __name__ == '__main__':
    main()
