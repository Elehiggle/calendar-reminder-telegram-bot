import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def ensure_data_directory(data_path):
    """Ensure the data directory exists."""
    os.makedirs(data_path, exist_ok=True)

def get_user_data_path(data_path, user_id):
    """Get the path for a specific user's data file."""
    user_dir = os.path.join(data_path, 'users')
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, f"user_{user_id}.json")

def save_user_reminders(data_path, user_id, reminders):
    """Save user reminders to disk."""
    file_path = get_user_data_path(data_path, user_id)
    
    # Convert data to serializable format
    serializable_reminders = {}
    for event_id, event_data in reminders.items():
        reminder_data = {
            'summary': event_data['summary'],
            'start_time': event_data['start_time'].isoformat(),
            'acknowledged': event_data['acknowledged'],
            'first_reminder': event_data.get('first_reminder', True)
        }
        
        # Handle next_reminder_time if it exists and is a datetime
        next_reminder = event_data.get('next_reminder_time')
        if next_reminder:
            if isinstance(next_reminder, datetime):
                reminder_data['next_reminder_time'] = next_reminder.isoformat()
            else:
                reminder_data['next_reminder_time'] = next_reminder
        else:
            reminder_data['next_reminder_time'] = ''
            
        serializable_reminders[event_id] = reminder_data
    
    try:
        with open(file_path, 'w') as f:
            json.dump(serializable_reminders, f, indent=2)
        logger.info(f"Saved {len(serializable_reminders)} reminders for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving reminders for user {user_id}: {e}")
        return False

def load_user_reminders(data_path, user_id):
    """Load user reminders from disk."""
    file_path = get_user_data_path(data_path, user_id)
    
    if not os.path.exists(file_path):
        logger.info(f"No saved reminders found for user {user_id}")
        return {}
    
    try:
        with open(file_path, 'r') as f:
            serialized_reminders = json.load(f)
        
        # Convert back to usable format
        reminders = {}
        for event_id, event_data in serialized_reminders.items():
            reminders[event_id] = {
                'summary': event_data['summary'],
                'start_time': datetime.fromisoformat(event_data['start_time']),
                'acknowledged': event_data['acknowledged'],
                'job': None
            }
            
            if 'next_reminder_time' in event_data and event_data['next_reminder_time']:
                reminders[event_id]['next_reminder_time'] = datetime.fromisoformat(event_data['next_reminder_time'])
            
            if 'first_reminder' in event_data:
                reminders[event_id]['first_reminder'] = event_data['first_reminder']
                
        logger.info(f"Loaded {len(reminders)} reminders for user {user_id}")
        return reminders
    except Exception as e:
        logger.error(f"Error loading reminders for user {user_id}: {e}")
        return {}

def load_all_users(data_path):
    """Load all user IDs from the data directory."""
    user_dir = os.path.join(data_path, 'users')
    if not os.path.exists(user_dir):
        return []
    
    user_ids = []
    for filename in os.listdir(user_dir):
        if filename.startswith("user_") and filename.endswith(".json"):
            try:
                user_id = int(filename[5:-5])  # Extract user_id from "user_XXXXX.json"
                user_ids.append(user_id)
            except ValueError:
                continue
    
    return user_ids
