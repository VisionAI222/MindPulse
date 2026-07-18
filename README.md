# MindPulse - AI Student Wellness Companion

MindPulse is an intelligent, full-stack mental health and wellness companion designed to provide students with a safe, confidential space to talk, process their thoughts, and receive empathetic support. The system monitors conversations in real-time, instantly surfacing localized crisis helplines and notifying authorized human counselors via automated administrative overrides if a severe life-safety risk is detected.

## 🚀 Live Links
* **Live Web Interface:** [PASTE_YOUR_GITHUB_PAGES_LINK_HERE]
* **Production API Backend:** [PASTE_YOUR_RENDER_URL_HERE]

## 🛠️ System Architecture
* **Frontend:** Responsive HTML5, CSS3 Grid layouts, and Vanilla JavaScript (`script.js`) managing asynchronous fetch streams.
* **Backend:** FastAPI (Python) web service deployed via Render handling secure webhook parsing, exception safety-valves, and response routing.
* **Safety & Triage Pipeline:** Dual-stage safety verification (Instant Scan & Deep Context Scan) engineered to detect high-risk trigger inputs, isolate Telegram API calls for unauthorized entities, and trigger manual counselor alerts (`COUNSELOR_CHAT_ID`).

## 📦 Project Directory Structure
├── index.html          # Web chat user interface layout
├── style.css           # Custom UI responsiveness stylesheets
├── script.js           # Stateful UI message rendering & API routing logic
├── main.py             # FastAPI routing, safety parsing, and data architecture
└── requirements.txt    # Production dependencies
