import os
import json
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile

import openai
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gtts import gTTS
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ASSISTANT_ID = os.getenv('ASSISTANT_ID')
CLIENT_SECRET_FILE = os.getenv('CLIENT_SECRET_FILE', 'client_secret.json')

# Check if OPENAI_API_KEY is set
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set. Please set it in the script or as an environment variable.")

# Set up OpenAI
client = openai.Client(api_key=OPENAI_API_KEY)

# Set up Google Calendar API
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

calendar_service = get_calendar_service()

# Initialize bot and dispatcher
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(f"Hello, {message.from_user.full_name}! I can help you with the following tasks:\n"
                         "1. Schedule meetings (start your message with 'Schedule')\n"
                         "2. Convert speech to text (send me an audio file)\n"
                         "3. Convert text to speech (send me text)")

async def process_meeting_request(message: str) -> dict:
    current_date = datetime.now().strftime("%Y-%m-%d")
    thread = client.beta.threads.create()
    
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=f"Current date: {current_date}\n\nSchedule: {message}"
    )

    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=ASSISTANT_ID,
        instructions=f"""
        Today's date is {current_date}. Extract meeting details from the user's message and return ONLY a JSON object with the following structure:
        {{
            "title": "Meeting title",
            "date": "YYYY-MM-DD",
            "time": "HH:MM",
            "duration": 60,
            "participants": ["name1", "name2"],
            "description": "Brief meeting description",
            "agenda": ["Item 1", "Item 2", "Item 3"]
        }}
        If any information is missing, use reasonable defaults or leave the field empty.
        For relative dates (e.g., "next Tuesday"), calculate the correct date based on the current date provided.
        Ensure the response is a valid JSON object and nothing else.
        """
    )

    while run.status != 'completed':
        await asyncio.sleep(1)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    messages = client.beta.threads.messages.list(thread_id=thread.id)
    
    try:
        content = messages.data[0].content[0].text.value
        logging.info(f"Raw response from OpenAI: {content}")
        response = json.loads(content)
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse JSON: {e}")
        logging.error(f"Raw content: {content}")
        response = {
            "title": "Default Meeting",
            "date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "time": "09:00",
            "duration": 60,
            "participants": [],
            "description": "Meeting details could not be extracted.",
            "agenda": ["Discuss meeting details"]
        }
    
    logging.info(f"Final processed meeting info: {response}")
    return response

def schedule_google_calendar_event(meeting_info: dict) -> tuple:
    start_time = datetime.strptime(f"{meeting_info['date']} {meeting_info['time']}", "%Y-%m-%d %H:%M")
    end_time = start_time + timedelta(minutes=meeting_info['duration'])

    event = {
        'summary': meeting_info['title'],
        'description': meeting_info['description'] + "\n\nAgenda:\n" + "\n".join(meeting_info['agenda']),
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Kolkata'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Kolkata'},
        'attendees': [{'email': f"{attendee.lower()}@example.com"} for attendee in meeting_info['participants']],
    }

    try:
        created_event = calendar_service.events().insert(calendarId='primary', body=event).execute()
        logging.info(f"Event created: {created_event.get('htmlLink')}")
        
        verified_event = calendar_service.events().get(calendarId='primary', eventId=created_event['id']).execute()
        logging.info(f"Event verified: {verified_event}")
        
        return created_event['htmlLink'], verified_event
    except HttpError as error:
        logging.error(f"An error occurred while creating the event: {error}")
        return None, None

async def transcribe_audio(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1", 
            file=audio_file
        )
    return transcript.text

async def text_to_speech(text):
    tts = gTTS(text=text, lang='en')
    audio_file = "output.mp3"
    tts.save(audio_file)
    return audio_file

@dp.message(F.content_type.in_({'voice', 'audio'}))
async def handle_audio(message: Message):
    try:
        file = await bot.get_file(message.voice.file_id if message.voice else message.audio.file_id)
        file_path = f"{file.file_id}.ogg"
        await bot.download_file(file.file_path, file_path)

        transcript = await transcribe_audio(file_path)
        await message.reply(f"Transcript: {transcript}")

        os.remove(file_path)
    except Exception as e:
        logging.error(f"Error processing audio: {str(e)}", exc_info=True)
        await message.reply("Sorry, I couldn't process the audio. Please try again.")

@dp.message(F.text)
async def handle_message(message: Message):
    try:
        if message.text.lower().startswith("schedule"):
            logging.info(f"Received meeting request: {message.text}")
            meeting_info = await process_meeting_request(message.text)
            calendar_link, verified_event = schedule_google_calendar_event(meeting_info)
            
            if calendar_link and verified_event:
                response = (f"Meeting scheduled!\n"
                            f"Title: {meeting_info['title']}\n"
                            f"Date: {meeting_info['date']}\n"
                            f"Time: {meeting_info['time']}\n"
                            f"Duration: {meeting_info['duration']} minutes\n"
                            f"Participants: {', '.join(meeting_info['participants'])}\n"
                            f"Description: {meeting_info['description']}\n"
                            f"Agenda:\n" + "\n".join(f"- {item}" for item in meeting_info['agenda']) +
                            f"\n\nCalendar link: {calendar_link}")
            else:
                response = "I'm sorry, I couldn't schedule the meeting in the calendar. Please check the logs for more information."
            
            await message.reply(response)
        else:
            audio_file = await text_to_speech(message.text)
            await message.reply_voice(FSInputFile(audio_file))
            os.remove(audio_file)
    except Exception as e:
        logging.error(f"Error processing message: {str(e)}", exc_info=True)
        await message.reply("Sorry, I couldn't process your request. Please try again.")

async def main() -> None:
    logging.info("Starting bot")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
