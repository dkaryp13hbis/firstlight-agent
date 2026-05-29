import os
from dotenv import load_dotenv

load_dotenv()

# SQL Server
SQL_SERVER   = os.getenv("SQL_SERVER", "192.168.100.7")
SQL_DATABASE = os.getenv("SQL_DATABASE", "bidata")
SQL_DRIVER   = os.getenv("SQL_DRIVER", "ODBC Driver 17 for SQL Server")
SQL_TRUSTED  = os.getenv("SQL_TRUSTED", "no")
SQL_USER     = os.getenv("SQL_USER", "")
SQL_PASSWORD = os.getenv("SQL_PASSWORD", "")

# Hotel
HOTEL_ID    = int(os.getenv("HOTEL_ID", "1"))
HOTEL_NAME  = os.getenv("HOTEL_NAME", "Pomegranate Wellness Spa Hotel")
TOTAL_ROOMS = int(os.getenv("HOTEL_TOTAL_ROOMS", "167"))

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Email (Gmail SMTP)
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER")        # your Gmail address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")    # Gmail App Password

SENDER_NAME     = os.getenv("SENDER_NAME", "FirstLight Morning Briefing")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")
RECIPIENT_NAME  = os.getenv("RECIPIENT_NAME", "General Manager")
