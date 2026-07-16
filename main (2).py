import os
import json
import datetime
import httpx
from fastapi import FastAPI, Request, HTTPException
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
Respond ONLY with valid JSON, no extra text, no markdown fences:
{"immediate_danger": true, "reason": "one short phrase"} or {"immediate_danger": false, "reason": "one short phrase"}"""

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

async def analyze_conversation(chat_id: str) -> dict:
    try:
        student_messages = [
            m["text"] for m in
            messages_collection.find({"chat_id": chat_id, "conversation_open": True}).sort("timestamp", 1)
        ]
        transcript = "\n".join(f"- {t}" for t in student_messages)

        completion = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.2,
            max_tokens=500,
            messages=[
                {"role": "system", "content": CONVERSATION_ANALYSIS_PROMPT},
                {"role": "user", "content": f"Conversation so far:\n{transcript}"},
            ],
        )
        raw = completion.choices[0].message.content.strip().strip("`").replace("json", "", 1).strip()
        result = json.loads(raw)
        assert result["risk_level"] in ("low", "moderate", "severe")
        return result
    except Exception:
        return {
            "risk_level": "moderate", "confidence": "low",
            "overall_mood_summary": "Parsing failure fallback.", "tone_shift": "unknown",
            "recurring_themes": [], "reasoning": "Fallback applied.",
            "clarifying_question": "Hey, how are you really feeling today?"
        }

async def generate_reply(latest_message: str, risk_level: str, mood_summary: str) -> str:
    style_guide = {
        "low": "Reply warmly and briefly. Offer ONE small practical grounding technique.",
        "moderate": "Reply with genuine warmth. Gently suggest talking to a counselor and offer to help book a slot."
    }
    completion = await groq_client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.7,
        max_tokens=200,
        messages=[
            {"role": "system", "content": f"You are a warm check-in companion. Context: {mood_summary}. {style_guide.get(risk_level, '')}"},
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
@app.get("/")
def health_check():
    return {"status": "MindPulse is running perfectly"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    message = body.get("message")
    if not message or "text" not in message:
        return {"ok": True}

    chat_id = str(message["chat"]["id"])
    student_text = message["text"]
    student_name = message["chat"].get("first_name", "")

    register_student(chat_id, student_name)
    save_message(chat_id, student_text)

    # Step 1: Instant Scan
    safety = await instant_safety_check(student_text)
    if safety["immediate_danger"]:
        await send_telegram_message(chat_id, CRISIS_HELPLINE_MESSAGE)
        await alert_human(chat_id, student_name, safety["reason"])
        close_conversation(chat_id)
        return {"ok": True}

    # Step 2: Deep Context Scan
    analysis = await analyze_conversation(chat_id)

    if analysis["risk_level"] == "severe":
        await send_telegram_message(chat_id, CRISIS_HELPLINE_MESSAGE)
        await alert_human(chat_id, student_name, analysis["reasoning"])
        close_conversation(chat_id)
    elif analysis["confidence"] == "low" and analysis.get("clarifying_question"):
        await send_telegram_message(chat_id, analysis["clarifying_question"])
        save_analysis(chat_id, analysis, analysis["clarifying_question"])
    else:
        reply_text = await generate_reply(student_text, analysis["risk_level"], analysis["overall_mood_summary"])
        await send_telegram_message(chat_id, reply_text)
        save_analysis(chat_id, analysis, reply_text)

    return {"ok": True}
