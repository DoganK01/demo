from email.mime.text import MIMEText
import base64
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials




def send_gmail(creds: Credentials, to: str, subject: str, body: str):
    service = build("gmail", "v1", credentials=creds)
    message = MIMEText(body, "plain")
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {"raw": raw}

    sent_msg = service.users().messages().send(userId="me", body=body).execute()
    print(f"📤 Email sent: ID {sent_msg['id']}")


def create_calendar_event(creds: Credentials, summary: str, start_time: datetime, duration_minutes: int = 60, location: str = None):
    """Creates an event on the user's primary calendar.

    Args:
        creds: The user's credentials.
        summary: The title of the event.
        start_time: The start time of the event (as a datetime object).
        duration_minutes: The duration of the event in minutes (default is 60).
        location: The location of the event (optional).
    """
    service = build("calendar", "v3", credentials=creds)
    end_time = start_time + timedelta(minutes=duration_minutes)

    event = {
        "summary": summary,
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "UTC", 
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "UTC",
        },
    }

    if location:
        event["location"] = location

    try:
        created_event = service.events().insert(calendarId="primary", body=event).execute()
        print(f"Event created: {created_event.get('htmlLink')}")
        return created_event
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
