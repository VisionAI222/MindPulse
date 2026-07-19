import os
import json
import datetime
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse
import time
from groq import AsyncGroq  # Switched to Async for FastAPI performance
from pymongo import MongoClient

# --- PRODUCTION ENVIRONMENT VARIABLE LOADING ---
# Do NOT use getpass here. Cloud Run injects these via its Settings UI.
COUNSELOR_CHAT_ID = os.environ.get("COUNSELOR_CHAT_ID", "@m1ndpuls3_bot")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MONGODB_URI = os.environ.get("MONGODB_URI")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Validate required environment variables
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set. Please set it to your Telegram bot token.")
if not MONGODB_URI:
    raise ValueError("MONGODB_URI environment variable not set. Please set it to your MongoDB connection string.")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY environment variable not set. Please set it to your Groq API key.")

app = FastAPI(title="MindPulse")
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows any frontend website to connect to your health check
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --- CLIENT INITIALIZATIONS ---
# Init Async Groq to prevent blocking requests
groq_client = AsyncGroq(api_key=GROQ_API_KEY)
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client["mindpulse"]
messages_collection = db["messages"]
analyses_collection = db["analyses"]
students_collection = db["students"]
SYSTEM_PROMPT = """
You are MindPulse, an empathetic, supportive, and intelligent AI companion designed for students. 

CRITICAL BEHAVIOR RULES:
1. Be a supportive listener first. Respond naturally to the user's specific text without forcing a preset agenda.
2. DO NOT constantly suggest breathing, grounding, or "5-4-3-2-1" exercises. Only offer tactical exercises if the user explicitly asks for a coping mechanism, expresses extreme acute panic, or asks how to calm down.
3. Keep your tone grounded, friendly, and human. Avoid sounding like a rigid, repetitive script. 
4. If a user says good night or hello, simply greet them warmly and conversationally without instructing them to take deep breaths or notice the ground.
"""
CHECKIN_MESSAGE = "Hey! Just checking in — how's your week been going? 🙂"
CRISIS_HELPLINE_MESSAGE = (
    "I hear you, and I'm really glad you told me. What you're feeling matters, and you "
    "don't have to go through this alone.\n\n"
    "Please reach out right now to someone who can help immediately:\n"
    "📞 iCall: 9152987821 (Mon-Sat, 10am-8pm)\n"
    "📞 AASRA: 9820466726 (24x7)\n"
    "📞 Vandrevala Foundation: 1860-2662-345 (24x7)\n\n"
    "A member of our support team has also been notified and will reach out to you."
)

INSTANT_SAFETY_PROMPT = """You are a fast safety scanner. Read ONE message from a student.

Your only question: does this message contain a DIRECT OR CLEARLY IMPLIED sign of
self-harm intent, suicidal thoughts, a wish to end their life, wanting to disappear
permanently, or immediate danger to their physical safety?

IMPORTANT: Simply naming an emotion or mental state - such as saying "I feel depressed",
"I'm sad", "I'm anxious", "I feel hopeless about my exam" - is NOT on its own a sign of
immediate danger. Everyday emotional language should NOT be flagged. Only flag when
there is an actual indication of self-harm, suicidal ideation, or intent to end their life.

Respond ONLY with valid JSON, no extra text, no markdown fences:
{"immediate_danger": true, "reason": "one short phrase"}
or
{"immediate_danger": false, "reason": "one short phrase"}

Examples of NOT immediate danger: "I'm feeling depressed today", "I feel so low lately",
"I'm really stressed and sad about everything".

Examples of immediate danger: "I want to end it all", "I don't want to be here anymore",
"I've been thinking about hurting myself", "life isn't worth living".

When genuinely ambiguous between everyday sadness and something more serious, lean toward
flagging - but a plain statement of feeling depressed, sad, or down, with nothing more,
is not enough on its own."""

CONVERSATION_ANALYSIS_PROMPT = """You are a mental health pattern analyst. Classify into risk levels: LOW, MODERATE, SEVERE.
Respond ONLY with valid JSON, no extra text, no markdown fences:
{
  "risk_level": "low",
  "confidence": "high",
  "overall_mood_summary": "summary text",
  "tone_shift": "stable",
  "recurring_themes": [],
  "reasoning": "reason text",
  "clarifying_question": ""
}"""
async def send_telegram_message(chat_id: str, text: str):
    if not TELEGRAM_BOT_TOKEN:
        print(f"[Mock Send] Token missing. Chat: {chat_id} | Text: {text}")
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()
        return resp.json()
async def instant_safety_check(message_text: str) -> dict:
    try:
        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.0,
            max_tokens=100,
            messages=[
                {"role": "system", "content": INSTANT_SAFETY_PROMPT},
                {"role": "user", "content": message_text},
            ],
        )
        raw = completion.choices[0].message.content.strip().strip("`").replace("json", "", 1).strip()
        result = json.loads(raw)
        return {"immediate_danger": bool(result["immediate_danger"]), "reason": result.get("reason", "")}
    except Exception as e:
        return {"immediate_danger": True, "reason": f"Safety check parsing fallback: {str(e)}"}

def clean_and_parse_json(raw_text: str) -> dict:
    import json
    try:
        # Strip away any markdown code block fences if the LLM added them
        cleaned = raw_text.strip().strip("`").replace("json", "", 1).strip()
        return json.loads(cleaned)
    except Exception:
        return {"risk_level": "low"}

async def analyze_conversation(chat_id: str) -> dict:
    try:
        # Fetch conversation open logs
        cursor = messages_collection.find({"chat_id": chat_id, "conversation_open": True}).sort("timestamp", 1)
        student_messages = [m["text"] for m in await cursor.to_list(length=100)]
        
        # FIX 1: If there are fewer than 2 messages, skip analysis and assume 'low' risk
        if len(student_messages) <= 1:
            return {
                "risk_level": "low", "confidence": "high",
                "overall_mood_summary": "Initial greeting or conversation opening.", "tone_shift": "stable",
                "recurring_themes": [], "reasoning": "New conversation sequence.", "clarifying_question": ""
            }

        transcript = "\n".join(f"- {t}" for t in student_messages)

        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.2, # 💡 Keep this low for reliable JSON generation
            max_tokens=500,
            messages=[
                {"role": "system", "content": CONVERSATION_ANALYSIS_PROMPT}, 
                {"role": "user", "content": f"Conversation so far:\n{transcript}"},
            ],
        )
        result = clean_and_parse_json(completion.choices[0].message.content)
        
        # FIX 2: Standardize case format to avoid strict assertion crash
        if "risk_level" in result:
            result["risk_level"] = str(result["risk_level"]).strip().lower()
            
        assert result.get("risk_level") in ("low", "moderate", "severe")
        return result
        
    except Exception as e:
        print(f"[Analysis Error Logged]: {str(e)}")
        # Safe structural fallback
        return {
            "risk_level": "low", "confidence": "low",
            "overall_mood_summary": "Parsing error structural handling.", "tone_shift": "unknown",
            "recurring_themes": [], "reasoning": "Fallback applied gracefully.", "clarifying_question": ""
        }        
async def generate_reply(latest_message: str, risk_level: str, mood_summary: str) -> str:
    style_guide = {
        "low": "Listen attentively. Respond casually and conversationally like an empathetic peer.",
        "moderate": "Reply with genuine warmth. Gently suggest talking to a counselor and offer to help book a slot."
    }
    
    completion = await groq_client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.7, # Keep it at 0.7 so it talks like a normal human being
        max_tokens=200,
        messages=[
            #  COMBINE: We pass the main SYSTEM_PROMPT instructions + specific risk context
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nCurrent Student Context: {mood_summary}. Guide: {style_guide.get(risk_level, '')}"},
            {"role": "user", "content": latest_message},
        ],
    )
    return completion.choices[0].message.content.strip()
def register_student(chat_id: str, name: str = ""):
    students_collection.update_one(
        {"chat_id": chat_id},
        {"$set": {"name": name}, "$setOnInsert": {"registered_at": datetime.datetime.utcnow()}},
        upsert=True
    )

def save_message(chat_id: str, text: str):
    messages_collection.insert_one({
        "chat_id": chat_id, "text": text, "timestamp": datetime.datetime.utcnow(), "conversation_open": True
    })

def save_analysis(chat_id: str, analysis: dict, reply: str):
    analyses_collection.insert_one({
        "chat_id": chat_id, "risk_level": analysis["risk_level"], "confidence": analysis["confidence"],
        "overall_mood_summary": analysis["overall_mood_summary"], "tone_shift": analysis.get("tone_shift", ""),
        "recurring_themes": analysis.get("recurring_themes", []), "reasoning": analysis["reasoning"],
        "clarifying_question": analysis.get("clarifying_question", ""), "reply": reply, "timestamp": datetime.datetime.utcnow()
    })

def close_conversation(chat_id: str):
    messages_collection.update_many({"chat_id": chat_id, "conversation_open": True}, {"$set": {"conversation_open": False}})

async def alert_human(chat_id: str, student_name: str, reason: str):
    alert_text = f"🚨 SEVERE risk flagged\nStudent: {student_name or 'Unknown'} ({chat_id})\nReason: {reason}"

    # 1. Get the raw string from environment variables
    raw_counselors = os.environ.get("COUNSELOR_CHAT_ID", "")

    if not raw_counselors:
        print("[Warning] No counselor chat IDs found in environment variables.")
        return

    # 2. Split the string by commas and strip out any accidental spaces
    counselor_ids = [c.strip() for c in raw_counselors.split(",") if c.strip()]

    # 3. Loop through and alert every single counselor/channel
    for counselor in counselor_ids:
        try:
            await send_telegram_message(counselor, alert_text)
        except Exception as e:
            # If sending to one counselor fails, log it but don't stop the loop
            print(f"[Alert Error] Failed to send alert to {counselor}: {str(e)}")

backend_logs = [
    f"{time.strftime('%H:%M:%S')} RENDER DETECTED ...",
    f"{time.strftime('%H:%M:%S')} INCOMING HTTP REQUEST DETECTED ...",
    f"{time.strftime('%H:%M:%S')} MINDPULSE AGENT IS ALREADY LIVE ...",
    "--------------------------------------------------"
]
@app.get("/get-raw-logs")
async def get_raw_logs():
    return Response(content="\n".join(backend_logs), media_type="text/plain")

@app.get("/", response_class=HTMLResponse)
async def live_terminal_url():
    html_layout = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Render Terminal</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { 
                background-color: #000000; 
                color: #ffffff; 
                font-family: monospace; 
                padding: 20px; 
                margin: 0; 
                white-space: pre-wrap;
                font-size: 14px;
                line-height: 1.5;
            }
        </style>
    </head>
    <body><div id="logs">Loading live stream...</div><script>
            async function refreshLogs() {
                try {
                    let res = await fetch('/get-raw-logs');
                    let text = await res.text();
                    document.getElementById('logs').innerText = text;
                    window.scrollTo(0, document.body.scrollHeight);
                } catch (e) { console.error(e); }
            }
            setInterval(refreshLogs, 2000);
            refreshLogs();
        </script></body>
    </html>
    """
    return HTMLResponse(content=html_layout, status_code=200)

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    web_reply = "Message received."
    
    # ID from Render environment variables (fallback to a dummy string if not set)
    ADMIN_CHAT_ID = os.environ.get("COUNSELOR_CHAT_ID", "99999") 

    try:
        body = await request.json()
        message = body.get("message")
        if not message or "text" not in message:
            return {"reply": "Invalid request structure."}

        chat_id = str(message["chat"]["id"])
        student_text = message["text"].strip()
        student_name = message["chat"].get("first_name", "Web User")
        
        is_web_client = (chat_id == "99999")

        # 📺 LOG INCOMING MESSAGE (No await!)
        backend_logs.append(f"{time.strftime('%H:%M:%S')} [INCOMING] Message from '{student_name}': {student_text}")

        # 1. Handle the Initial Start Command Explicitly
        if student_text == "/start":
            welcome_msg = "Welcome to MindPulse. How are you doing today?"
            if not is_web_client:
                await send_telegram_message(chat_id, welcome_msg)
            try:
                register_student(chat_id, student_name)
            except Exception as e:
                print(f"[Register Error Ignored]: {e}")
            
            backend_logs.append(f"{time.strftime('%H:%M:%S')} [SYSTEM] New conversation session started.")
            return {"reply": welcome_msg}

        # 2. Run Database Logging Safely
        try:
            register_student(chat_id, student_name)
            save_message(chat_id, student_text)
        except Exception as db_err:
            print(f"[Database Log Error]: {db_err}")

        # 3. Instant Scan Pipeline
        try:
            safety = await instant_safety_check(student_text)
            if safety.get("immediate_danger"):
                backend_logs.append(f"{time.strftime('%H:%M:%S')} [SAFETY] CRITICAL FLAG: Immediate danger detected!")
                # Route to you if it's the web client, otherwise to the actual chat user
                target_admin_id = ADMIN_CHAT_ID if is_web_client else chat_id
                
                await alert_human(target_admin_id, student_name, safety.get("reason", "Immediate risk flag."))
                
                if not is_web_client:
                    await send_telegram_message(chat_id, CRISIS_HELPLINE_MESSAGE)
                    try:
                        close_conversation(chat_id)
                    except:
                        pass
                return {"reply": CRISIS_HELPLINE_MESSAGE}
        except Exception as safety_err:
            print(f"[Safety Engine Error]: {safety_err}")

        # 4. Deep Context Scan Pipeline (THE FIX: Removed 'await' from analyze_conversation)
        try:
            backend_logs.append(f"{time.strftime('%H:%M:%S')} [ANALYSIS] Running transformer context evaluations...")
            analysis = analyze_conversation(chat_id)
        except Exception as analysis_err:
            print(f"[Analysis Core Intercepted]: {analysis_err}")
            analysis = {"risk_level": "low", "confidence": "low", "overall_mood_summary": "Default conversational stream."}

        # 5. Routing Options & Processing
        if analysis.get("risk_level") == "severe":
            backend_logs.append(f"{time.strftime('%H:%M:%S')} [SAFETY] SEVERE LONG-TERM RISK FLAG ENCOUNTERED.")
            target_admin_id = ADMIN_CHAT_ID if is_web_client else chat_id
            await alert_human(target_admin_id, student_name, analysis.get("reasoning", "Severe risk detected."))
            
            if not is_web_client:
                await send_telegram_message(chat_id, CRISIS_HELPLINE_MESSAGE)
                try:
                    close_conversation(chat_id)
                except:
                    pass
            web_reply = CRISIS_HELPLINE_MESSAGE
            
        elif analysis.get("confidence") == "low" and analysis.get("clarifying_question"):
            fallback_phrase = "Hey, how are you really feeling today?"
            reply_text = analysis.get("clarifying_question", fallback_phrase)
            
            if not is_web_client:
                await send_telegram_message(chat_id, reply_text)
            
            web_reply = reply_text
        else:
            try:
                reply_text = await generate_reply(student_text, analysis.get("risk_level", "low"), analysis.get("overall_mood_summary", "Conversing"))
            except Exception as e:
                print(f"[Reply Generation Error]: {e}")
                reply_text = "I'm right here with you. Can you describe a bit more about what you're experiencing?"
                
            if not is_web_client:
                await send_telegram_message(chat_id, reply_text)
                
            web_reply = reply_text

        # 📺 LOG SUCCESSFUL RESPONSE OUTBOUND
        backend_logs.append(f"{time.strftime('%H:%M:%S')} [SUCCESS] Sent response back to chat interface.")

    except Exception as global_err:
        print(f"[CRITICAL GLOBAL WEBHOOK ERROR]: {global_err}")
        backend_logs.append(f"{time.strftime('%H:%M:%S')} [CRITICAL ERROR] Webhook execution failed.")
        return {"reply": "Processing pipeline error. Check server logs."}
    
    return {"reply": web_reply}
