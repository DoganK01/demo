import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
import websockets
from dotenv import load_dotenv
import uvicorn
import re
from google_helper import get_google_creds
from google_services import send_gmail, create_calendar_event
from google import genai
from datetime import datetime, timedelta
from pyngrok import ngrok

from constants import SYSTEM_PROMPT_REFINED, SYSTEM_PROMPT_GEMINI
from utils import render_input_nicely
load_dotenv()

GEMINI_API = os.getenv("GEMINI_API")
TWILIO_ACCOUNT_SID=os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN=os.getenv("TWILIO_AUTH_TOKEN")
PHONE_NUMBER_FROM=os.getenv("PHONE_NUMBER_FROM")
OPENAI_API_KEY= os.getenv("OPENAI_API_KEY")
OPENAI_KEY=os.getenv("OPENAI_API_KEY")
PORT=7070
RAW_NGROK_URL = None

SHOW_TIMING_MATH = True

try:
    print("Starting ngrok tunnel...")
    ngrok_tunnel = ngrok.connect(PORT, bind_tls=True)
    raw_domain = ngrok_tunnel.public_url
    RAW_NGROK_URL = raw_domain
    DOMAIN = re.sub(r'(^\w+:|^)\/\/|\/+$', '', raw_domain)
    print(f"Ngrok tunnel active at: {DOMAIN}")
except Exception as e:
    raise RuntimeError(f"Failed to start ngrok tunnel: {e}")



VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]

app = FastAPI()

if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and PHONE_NUMBER_FROM and OPENAI_KEY):
    raise ValueError('Missing Twilio and/or OpenAI environment variables. Please set them in the .env file.')

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API)
chat_log = []

@app.get('/', response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.get("/ngrok-status")
async def ngrok_status():
    return {"ngrok_url": DOMAIN}

@app.websocket('/media-stream')
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()
    try:
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_KEY}",
                "OpenAI-Beta": "realtime=v1"
            },
            open_timeout=10,
            ping_timeout=20,

        ) as openai_ws:
            await initialize_session(openai_ws)
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None


            async def receive_from_twilio():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data['event'] == 'media' and openai_ws.open:
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            print(f"Incoming stream has started {stream_sid}")
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                        elif data['event'] == 'mark':
                            if mark_queue:
                                mark_queue.pop(0)
                        elif data['event'] == "stop":
                            call_sid = data.get("stop", {}).get("callSid")
                            print(f"Call stopped: {call_sid}")
                            try:
                                await cleanup_and_execute_google_services(call_sid)
                            except Exception as e:
                                print(f"Error during cleanup and Google service execution: {e}")
                            break
                except WebSocketDisconnect:
                    print("Client disconnected.")
                    if openai_ws.open:
                        await openai_ws.close()
                    raise

            async def send_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        if response['type'] == "conversation.item.created":
                            if response['item']['role'] == "user":
                                for content in response['item']['content']:
                                    if content['type'] == "input_text":
                                        current_user_text = content['text']
                                        if current_user_text:
                                            chat_log.append(f"User: {current_user_text}")
                        if response['type'] == "response.output_item.done":
                            if response['item']['role'] == "assistant":
                                for content in response['item']['content']:
                                    if content['type'] == "audio" and content['transcript']:
                                        current_assistant_response = content['transcript']
                                        if current_assistant_response:
                                            chat_log.append(f"Assistant: {current_assistant_response}")
                        if response['type'] in LOG_EVENT_TYPES:
                            print(f"Received event: {response['type']}", response)
                        if response['type'] == 'session.updated':
                            print("Session updated successfully:", response)
                        if response['type'] == 'response.audio.delta' and response.get('delta'):
                            try:
                                audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": audio_payload
                                    }
                                }
                                await websocket.send_json(audio_delta)
                                if response_start_timestamp_twilio is None:
                                    response_start_timestamp_twilio = latest_media_timestamp
                                    if SHOW_TIMING_MATH:
                                        print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                                if response.get('item_id'):
                                    last_assistant_item = response['item_id']
                                
                                await send_mark(websocket, stream_sid)
                            except Exception as e:
                                print(f"Error processing audio delta: {e}")
                                if "WebSocket is closed" in str(e) or isinstance(e, WebSocketDisconnect):
                                    raise WebSocketDisconnect
                                else:
                                    continue
                        if response.get('type') == 'input_audio_buffer.speech_started':
                            print("Speech started detected.")
                            if last_assistant_item:
                                print(f"Interrupting response with id: {last_assistant_item}")
                                await handle_speech_started_event()
                except Exception as e:
                    print(f"Error in send_to_twilio: {e}")
                    if "WebSocket is closed" in str(e) or isinstance(e, WebSocketDisconnect):
                        raise WebSocketDisconnect

            async def handle_speech_started_event():
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                print("Handling speech started event.")
                if mark_queue and response_start_timestamp_twilio is not None:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if SHOW_TIMING_MATH:
                        print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                    if last_assistant_item:
                        if SHOW_TIMING_MATH:
                            print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                        truncate_event = {
                            "type": "conversation.item.truncate",
                            "item_id": last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms": elapsed_time
                        }
                        await openai_ws.send(json.dumps(truncate_event))

                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid
                    })

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None

            async def send_mark(connection, stream_sid):
                if stream_sid:
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "responsePart"}
                    }
                    await connection.send_json(mark_event)
                    mark_queue.append('responsePart')

            await asyncio.gather(receive_from_twilio(), send_to_twilio(), return_exceptions=False)
    except WebSocketDisconnect:
        print("WebSocket disconnected, closing connections...")

        if openai_ws and openai_ws.open:
            await openai_ws.close()
            print("OpenAI WebSocket closed.")

        try:
            await websocket.close()
            print("Twilio WebSocket closed.")
        except Exception as e:
            print(f"Error closing Twilio WebSocket: {e}")

        try:
            ngrok.disconnect(RAW_NGROK_URL)
            print("Ngrok tunnel disconnected.")
        except Exception as e:
            print(f"Error disconnecting ngrok: {e}")

async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_PROMPT_REFINED,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


async def check_number_allowed(to):
    """Check if a number is allowed to be called."""
    try:

        incoming_numbers = client.incoming_phone_numbers.list(phone_number=to)
        if incoming_numbers:
            return True

        outgoing_caller_ids = client.outgoing_caller_ids.list(phone_number=to)
        if outgoing_caller_ids:
            return True

        return False
    except Exception as e:
        print(f"Error checking phone number: {e}")
        return False
    
async def make_call(phone_number_to_call: str):
    """Make an outbound call."""
    if not phone_number_to_call:
        raise ValueError("Please provide a phone number to call.")

    is_allowed = await check_number_allowed(phone_number_to_call)
    if not is_allowed:
        raise ValueError(f"The number {phone_number_to_call} is not recognized as a valid outgoing number or caller ID.")


    outbound_twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{DOMAIN}/media-stream" /></Connect></Response>'
    )

    call = client.calls.create(
        from_=PHONE_NUMBER_FROM,
        to=phone_number_to_call,
        twiml=outbound_twiml
    )

    await log_call_sid(call.sid)

async def log_call_sid(call_sid):
    """Log the call SID."""
    print(f"Call started with SID: {call_sid}")


async def cleanup_and_execute_google_services(call_sid: str):
    print(f"Running post-call cleanup for Call SID: {call_sid}")
    global chat_log
    try:
        creds = get_google_creds()
        response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-preview-05-20",
        contents=SYSTEM_PROMPT_GEMINI.format(chat_log=chat_log))
        send_gmail(
            creds,
            to=os.getenv("RECIPIENT"),
            subject="Reservation Report for April 26th Dinner",
            body=render_input_nicely(response.text)
        )

        create_calendar_event(
            creds,
            summary=f"Review AI Chat Transcript (Call SID: {call_sid})",
            start_time=datetime.utcnow() + timedelta(hours=1),
            duration_minutes=30,
        )
    except Exception as e:
        print(f"Failed to run Google service tasks: {e}")

if __name__ == "__main__":

    phone_number=os.getenv("phone_number")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(make_call(phone_number))

    uvicorn.run(app, host="0.0.0.0", port=PORT)
