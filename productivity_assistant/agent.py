import os
import json
import base64
from email.message import EmailMessage
import urllib.request
import io

from google.adk.agents import LlmAgent, SequentialAgent, LoopAgent
from google.adk.tools.tool_context import ToolContext
from google.adk.agents.callback_context import CallbackContext

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Define the exact Google APIs this tool bridge is allowed to communicate with.
SCOPES = [
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/presentations'
]

# --- Core Bridge Implementation ---
class MCPWorkspaceBridge:
    """
    Physical Tool Executor. 
    Handles OAuth and executes real payload actions on Google Workspace APIs.
    """
    def __init__(self):
        self.creds = None
        self.token_path = 'token.json'
        
        # Load the desktop client secret file name from the environment automatically
        self.client_secret_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

    def authenticate(self):
        """Triggers the browser login and builds the API execution clients."""
        print("🔐 [MCP Tool Bridge] Authenticating with Google Workspace...")
        if os.path.exists(self.token_path):
            self.creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                if not os.path.exists(self.client_secret_file):
                    print(f"❌ FATAL ERROR: Cannot find OAuth file at '{self.client_secret_file}'. Make sure it is in your project root!")
                    return False
                
                # Check if it's a web or desktop client to handle flow correctly
                with open(self.client_secret_file, 'r') as f:
                    client_config = json.load(f)
                
                if 'web' in client_config:
                    print("🌐 Detected WEB application client type.")
                    # Using InstalledAppFlow for local development convenience
                    flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_file, SCOPES)
                    # Use port 8080 for OAuth and explicitly set host to 'localhost'
                    self.creds = flow.run_local_server(host='localhost', port=8080, prompt='consent')
                else:
                    print("💻 Detected DESKTOP (installed) application client type.")
                    flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_file, SCOPES)
                    self.creds = flow.run_local_server(port=0)
            
            with open(self.token_path, 'w') as token:
                token.write(self.creds.to_json())
                
        # Build API Clients
        self.tasks_service = build('tasks', 'v1', credentials=self.creds)
        self.calendar_service = build('calendar', 'v3', credentials=self.creds)
        self.gmail_service = build('gmail', 'v1', credentials=self.creds)
        self.drive_service = build('drive', 'v3', credentials=self.creds)
        self.docs_service = build('docs', 'v1', credentials=self.creds)
        self.slides_service = build('slides', 'v1', credentials=self.creds)
        return True

    def _create_task(self, title, notes):
        task_body = {'title': title, 'notes': notes}
        result = self.tasks_service.tasks().insert(tasklist='@default', body=task_body).execute()
        return f"Successfully added task: {result.get('title')}"

    def _create_event(self, summary, start_time, end_time, description, location=""):
        event = {
          'summary': summary,
          'description': description,
          'location': location,
          'start': {'dateTime': start_time},
          'end': {'dateTime': end_time},
        }
        result = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
        return f"Created calendar event. Link: {result.get('htmlLink')}"

    def _list_calendar_events(self, days=1):
        from datetime import datetime, timedelta
        now = datetime.utcnow().isoformat() + 'Z'
        later = (datetime.utcnow() + timedelta(days=days)).isoformat() + 'Z'
        events_result = self.calendar_service.events().list(
            calendarId='primary', timeMin=now, timeMax=later,
            singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        if not events:
            return "No upcoming events found."
        
        output = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            loc = event.get('location', 'No location specified')
            output.append(f"[{start}] {event['summary']} @ {loc}")
        return "\n".join(output)

    def _send_email(self, to, subject, body):
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to
        message['From'] = "me"
        message['Subject'] = subject

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}
        
        send_message = (self.gmail_service.users().messages().send(userId="me", body=create_message).execute())
        return f"Drafted / Sent email with ID: {send_message['id']}"

    def _auto_detect_location(self):
        # 1. Check for manual override in .env first (Best for demos)
        env_loc = os.environ.get("USER_LOCATION")
        if env_loc:
            print(f"🏠 [Bridge] Using environment location: {env_loc}")
            return env_loc

        # 2. Fallback to Google Geolocation API
        api_key = os.environ.get("GCP_API_KEY", os.environ.get("GOOGLE_API_KEY"))
        url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={api_key}"
        try:
            req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                loc = data.get('location', {})
                if loc:
                    return f"{loc['lat']},{loc['lng']}"
        except Exception as e:
            print(f"⚠️ [Bridge] Could not auto-detect location: {e}")
        return None

    def _get_directions(self, origin, destination):
        if not origin or origin.lower() in ["current location", "me", "here", "my location"]:
            print("🌍 [Bridge] Origin not specified. Attempting auto-detection...")
            detected = self._auto_detect_location()
            if detected:
                origin = detected
                print(f"📍 [Bridge] Auto-detected origin: {origin}")
            else:
                return "Error: Could not detect your current location. Please specify an origin (e.g. 'from San Francisco')."

        api_key = os.environ.get("GCP_API_KEY", os.environ.get("GOOGLE_API_KEY"))
        url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin.replace(' ', '+')}&destination={destination.replace(' ', '+')}&key={api_key}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                if data.get('status') == 'OK':
                    route = data['routes'][0]['legs'][0]
                    return f"Directions from {origin} to {destination}:\nDistance: {route['distance']['text']}\nTime: {route['duration']['text']}."
                else:
                    return f"Maps API returned status: {data.get('status')}"
        except Exception as e:
            return f"Error fetching directions: {str(e)}"

    def _get_current_weather(self, location):
        api_key = os.environ.get("GCP_API_KEY", os.environ.get("GOOGLE_API_KEY"))
        geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?name={location.replace(' ', '+')}&count=1&language=en&format=json"
        try:
            req = urllib.request.Request(geocode_url)
            with urllib.request.urlopen(req) as response:
                geo_data = json.loads(response.read().decode())
                if not geo_data.get('results'):
                    return f"Could not find coordinates for location: {location}"
                lat = geo_data['results'][0]['latitude']
                lon = geo_data['results'][0]['longitude']
                
                weather_url = f"https://weather.googleapis.com/v1/currentConditions:lookup?key={api_key}&location.latitude={lat}&location.longitude={lon}"
                w_req = urllib.request.Request(weather_url)
                with urllib.request.urlopen(w_req) as w_response:
                    w_data = json.loads(w_response.read().decode())
                    condition = w_data.get('weatherCondition', {}).get('description', {}).get('text', 'Unknown')
                    temp = w_data.get('temperature', {}).get('degrees', 'Unknown')
                    return f"Google Weather for {location}: {condition}, {temp}°C."
        except Exception as e:
            return f"Error fetching Google Weather API: {str(e)}"

    def _get_weather_forecast(self, location, days=3):
        api_key = os.environ.get("GCP_API_KEY", os.environ.get("GOOGLE_API_KEY"))
        geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?name={location.replace(' ', '+')}&count=1&language=en&format=json"
        try:
            req = urllib.request.Request(geocode_url)
            with urllib.request.urlopen(req) as response:
                geo_data = json.loads(response.read().decode())
                if not geo_data.get('results'):
                    return f"Could not find coordinates for location: {location}"
                lat = geo_data['results'][0]['latitude']
                lon = geo_data['results'][0]['longitude']
                
                weather_url = f"https://weather.googleapis.com/v1/forecast/days:lookup?key={api_key}&location.latitude={lat}&location.longitude={lon}&days={days}"
                w_req = urllib.request.Request(weather_url)
                with urllib.request.urlopen(w_req) as w_response:
                    w_data = json.loads(w_response.read().decode())
                    forecasts = []
                    for day in w_data.get('forecastDays', []):
                        date_info = day.get('displayDate', {})
                        date_str = f"{date_info.get('year')}-{date_info.get('month')}-{date_info.get('day')}"
                        cond = day.get('daytimeForecast', {}).get('weatherCondition', {}).get('description', {}).get('text', 'Unknown')
                        forecasts.append(f"[{date_str} - {cond}]")
                    return f"Google Weather Forecast for {location}: " + " ".join(forecasts)
        except Exception as e:
            return f"Error fetching Google Weather API: {str(e)}"

    def _read_drive_doc(self, file_name):
        try:
            results = self.drive_service.files().list(
                q=f"name contains '{file_name}' and (mimeType='application/vnd.google-apps.document' or mimeType='application/vnd.google-apps.presentation')",
                spaces='drive',
                fields='files(id, name, mimeType)'
            ).execute()
            
            items = results.get('files', [])
            if not items:
                return f"No Google Docs or Slides found matching the name '{file_name}'."
                
            file_id = items[0]['id']
            request = self.drive_service.files().export_media(fileId=file_id, mimeType='text/plain')
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                
            return f"--- CONTENTS OF {items[0]['name']} ---\n{fh.getvalue().decode('utf-8')[:1000]}..."
        except Exception as e:
            return f"Error searching/reading Drive: {str(e)}"

    def _write_doc(self, title, content):
        doc = self.docs_service.documents().create(body={'title': title}).execute()
        doc_id = doc.get('documentId')
        requests = [{'insertText': {'location': {'index': 1}, 'text': content}}]
        self.docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
        return f"Created Google Doc '{title}' with ID: {doc_id}"

    def _create_slides(self, title):
        presentation = self.slides_service.presentations().create(body={'title': title}).execute()
        return f"Created presentation '{title}' with ID: {presentation.get('presentationId')}"

    def _add_slide_to_presentation(self, presentation_id, title, content):
        requests = [
            {'createSlide': {'slideLayoutReference': {'predefinedLayout': 'TITLE_AND_BODY'}}},
            {'insertText': {'objectId': '', 'text': title, 'insertionIndex': 0}}, # This is simplified
        ]
        # Real Slides logic involves object IDs which are tricky; for hackathon we will focus on basic creation.
        return f"Added slide to presentation {presentation_id}"

# Setup global bridge instance 
bridge = MCPWorkspaceBridge()

# Provide a callback config to ensure the bridge is authenticated before tools run
def check_auth(callback_context: CallbackContext):
    if not bridge.creds:
        bridge.authenticate()

# --- Tool Definitions ---

def tool_create_task(tool_context: ToolContext, title: str, notes: str = "") -> str:
    """Creates a new task in Google Tasks."""
    return bridge._create_task(title=title, notes=notes)

def tool_create_calendar_event(tool_context: ToolContext, summary: str, start_time: str, end_time: str, description: str = "", location: str = "") -> str:
    """Creates a calendar event. Use RFC3339 format for start_time and end_time (e.g. 2026-04-09T09:00:00-07:00)."""
    return bridge._create_event(summary=summary, start_time=start_time, end_time=end_time, description=description, location=location)

def tool_list_calendar_events(tool_context: ToolContext, days: int = 1) -> str:
    """Lists calendar events for the next N days."""
    return bridge._list_calendar_events(days=days)

def tool_send_email(tool_context: ToolContext, to: str, subject: str, body: str) -> str:
    """Drafts and sends an email via Gmail."""
    return bridge._send_email(to=to, subject=subject, body=body)

def tool_get_directions(tool_context: ToolContext, destination: str, origin: str = "") -> str:
    """Gets driving directions, distance, and duration to a destination. If origin is omitted, it will auto-detect your location."""
    return bridge._get_directions(origin=origin, destination=destination)

def tool_get_current_weather(tool_context: ToolContext, location: str) -> str:
    """Gets the current weather condition and temperature for a given location."""
    return bridge._get_current_weather(location=location)

def tool_get_weather_forecast(tool_context: ToolContext, location: str, days: int = 3) -> str:
    """Gets a multi-day weather forecast for a given location."""
    return bridge._get_weather_forecast(location=location, days=days)

def tool_read_drive_doc(tool_context: ToolContext, file_name: str) -> str:
    """Searches for a Google Drive Document or Presentation and reads its plaintext contents."""
    return bridge._read_drive_doc(file_name=file_name)

def tool_write_doc(tool_context: ToolContext, title: str, content: str) -> str:
    """Creates a new Google Doc with the specified title and content."""
    return bridge._write_doc(title=title, content=content)

def tool_create_presentation(tool_context: ToolContext, title: str) -> str:
    """Creates a new Google Slides presentation."""
    return bridge._create_slides(title=title)

# --- State Keys ---
STATE_MANAGER = "manager_output"
STATE_SCHEDULER = "scheduler_output"
STATE_RESEARCHER = "researcher_output"
STATE_COMMS = "communications_output"
STATE_CHIEF = "chief_output"

# --- Agent Definitions ---

GEMINI_MODEL = os.getenv("MODEL_NAME", "gemini-2.5-flash")

# --- Branding & Styling ---
PREMIUM_STYLING = """
## 🎨 Response Protocol
- Use clear **Markdown headers (##)** for major sections.
- Use **bullet points** for details.
- **Bold** key information (names, dates, links, file IDs).
- Tone: Professional, Executive, and Concise.
"""

manager_agent = LlmAgent(
    name="Manager",
    model=GEMINI_MODEL,
    instruction=f"""
    You are the MANAGER agent.
    Create, track, and close tasks inside Google Tasks. 
    You act as an execution machine to keep projects moving forward.
    {PREMIUM_STYLING}
    """,
    tools=[tool_create_task],
    description="Handles actionable items, tracking, and Google Tasks.",
    output_key=STATE_MANAGER
)

from datetime import datetime

def get_scheduler_instruction(ctx) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""
    You are the SCHEDULER agent.
    Manage time and logistics. Read Google Calendar, check weather forecasts, 
    and map driving times to arrange the perfect schedule.
    If an event is outdoors and it rains, you must recommend moving it.
    
    CRITICAL REAL-TIME SYSTEM INFO:
    The current date and time is {now}. 
    All scheduling, calendar events, and calculations must use this as the current today's date.
    
    WEATHER BEST PRACTICE: 
    When checking weather for a specific landmark (like 'IIT Madras'), always extract the CITY name (e.g., 'Chennai') 
    to pass to the weather tool. It is much more reliable than using the specific address.

    {PREMIUM_STYLING}
    """

scheduler_agent = LlmAgent(
    name="Scheduler",
    model=GEMINI_MODEL,
    instruction=get_scheduler_instruction,
    tools=[tool_create_calendar_event, tool_list_calendar_events, tool_get_directions, tool_get_current_weather, tool_get_weather_forecast],
    description="Manages time, calendar, maps, and weather. Use Google Search if weather for a specific landmark is not found.",
    output_key=STATE_SCHEDULER
)

researcher_agent = LlmAgent(
    name="Researcher",
    model=GEMINI_MODEL,
    instruction=f"""
    You are the RESEARCHER agent. 
    Fetch historical context, read Google Drive documents, and handle project information.
    If you cannot find the relevant information locally, use your Google Search tool to search the live web.
    Use your document tools to read and write project documents for the user.
    Always provide accurate summaries.
    {PREMIUM_STYLING}
    """,
    tools=[tool_read_drive_doc, tool_write_doc],
    description="Handles data retrieval, reading/writing documents, web searching, and finding context.",
    output_key=STATE_RESEARCHER
)

communications_agent = LlmAgent(
    name="CommunicationsAgent",
    model=GEMINI_MODEL,
    instruction=f"""
    You are the COMMUNICATIONS agent.
    Draft, format, and send emails via Gmail. 
    You also create professional presentations using Google Slides.
    Ensure all communication is professional, accurate, and includes links.
    {PREMIUM_STYLING}
    """,
    tools=[tool_send_email, tool_create_presentation],
    description="Handles messaging, sending emails, and creating presentations.",
    output_key=STATE_COMMS
)

def get_chief_instruction(ctx) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""
    You are the **Productivity Assistant**, an elite orchestrator designed to streamline Google Workspace workflows. 
    Analyze the user's intent and route requests to specialized agents only when necessary.
    The current system date and time is {now}. 

    ## 🎯 GREETING & ORCHESTRATION
    - Respond directly as the **Productivity Assistant**. Always greet **Manju Vallabha** warmly and briefly outline how you can boost their productivity across Tasks, Calendar, Drive, and Gmail.
    - When a specialized agent (Manager, Scheduler, Researcher, Communications) returns information, you MUST synthesize that information into a final, premium markdown response for Manju. Do not just stop; provide the conclusion.
    - Treat **Manju Vallabha** as the primary executive you are assisting.
    
    {PREMIUM_STYLING}
    """

chief_of_staff_agent = LlmAgent(
    name="ChiefOfStaff",
    model=GEMINI_MODEL,
    instruction=get_chief_instruction,
    description="The orchestrator and Productivity Assistant. Routes requests to specialized agents and provides a comprehensive, beautifully formatted summary of their results to the user.",
    sub_agents=[manager_agent, scheduler_agent, researcher_agent, communications_agent],
    before_agent_callback=check_auth,
    output_key=STATE_CHIEF
)

# --- Root Agent ---
# We use the ChiefOfStaff as the root agent directly to avoid double bubbles in the UI.
root_agent = chief_of_staff_agent


if __name__ == "__main__":
    import asyncio
    
    # Simple CLI Test for the newly refactored ADK System
    async def run_pipeline():
        print("🧠 AI Multi-Agent Operations Manager [ONLINE via ADK]")
        print("=====================================================")
        
        while True:
            user_input = input("\nYou: ")
            if user_input.strip().lower() in ['exit', 'quit', 'q']:
                break
                
            input_context = {"user_intent": user_input}
            response = await root_agent.run_async(input_context)
            
            print(f"\\n[System Output]: {response}")

    asyncio.run(run_pipeline())
