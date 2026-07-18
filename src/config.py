import os
from dotenv import load_dotenv

load_dotenv()

# Gmail
GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
NEWSLETTER_SENDER_EMAIL = os.environ["NEWSLETTER_SENDER_EMAIL"]
NEWSLETTER_SUBJECT_KEYWORD = os.getenv("NEWSLETTER_SUBJECT_KEYWORD", "")

# Slack
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_ANNOUNCE_CHANNEL = os.environ["SLACK_ANNOUNCE_CHANNEL"]

# Gemini
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Calendar (optional — public Google Calendar iCal feeds, comma-separated)
CALENDAR_ICAL_URLS = [u.strip() for u in os.getenv("CALENDAR_ICAL_URLS", "").split(",") if u.strip()]
CALENDAR_INFO_URL = os.getenv("CALENDAR_INFO_URL", "")
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "America/New_York")

# Storage
DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "newsletters.db")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma")
