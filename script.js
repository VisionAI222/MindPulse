// ===============================================
// MindPulse AI Configuration & Connection Configs
// ===============================================
const BACKEND_URL = "https://mindpulse-gw43.onrender.com";
const CHAT_URL = "https://mindpulse-gw43.onrender.com/telegram-webhook";

// Show clean layout greeting bubble immediately upon page mount
window.onload = () => {
    checkStatus();
    
    const reply = document.getElementById("reply");
    reply.innerHTML = `
        <div class="aiBubble ai-msg">
            👋 <b>Welcome to MindPulse</b><br><br>
            I am your absolute private, safe space. Share how you're feeling today, or let me know if there's any stress weighing you down.
        </div>
    `;
};

// Periodic health checks against the Render backend API 
async function checkStatus(){
    const dot = document.getElementById("statusDot");
    const text = document.getElementById("statusText");

    try {
        const response = await fetch(BACKEND_URL);
        if(!response.ok) throw new Error();
        dot.style.background = "#22c55e";
        text.innerHTML = "🟢 AI Engine Online";
    } catch {
        dot.style.background = "#ef4444";
        text.innerHTML = "🔴 Backend Offline";
    }
}

// Main processing sequence for outgoing user messaging
async function send(){
    const input = document.getElementById("message");
    const reply = document.getElementById("reply");
    const message = input.value.trim();

    if(message === "") return;

    // Append user's chat bubble text
    reply.innerHTML += `
        <div class="userBubble">
            ${message}
        </div>
    `;

    // Instantiate typing loader component
    reply.innerHTML += `
        <div class="typing" id="typing">
            <span></span>
            <span></span>
            <span></span>
        </div>
    `;
    
    // Auto scroll down to match active input focus 
    reply.scrollTop = reply.scrollHeight;
    input.value = "";

    try {
        const response = await fetch(CHAT_URL, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                message: {
                    chat: { id: "99999" },
                    text: message
                }
            })
        });

        const data = await response.json();
        
        // Remove typing indicator safely
        const typingIndicator = document.getElementById("typing");
        if(typingIndicator) typingIndicator.remove();

        /* 
           🤖 SYNCHRONIZED BOT PARSING LAYER:
           Extracts the live token generation payload directly out of the 
           active server request return strings before handling fallbacks.
        */
        let aiTextResponse = "";
        
        if (data) {
            // Check if your FastAPI returned the string object inside standard parameters
            if (data.reply) aiTextResponse = data.reply;
            else if (data.text) aiTextResponse = data.text;
            else if (data.response) aiTextResponse = data.response;
            else if (data.message) aiTextResponse = data.message;
            // Handle cases where the backend directly streams a raw text block string
            else if (typeof data === "string") aiTextResponse = data;
            // Fallback strategy if the backend successfully executes but keeps output properties hidden
            else aiTextResponse = "Message routed successfully to your Telegram bot channel!";
        } else {
            aiTextResponse = "No response payload received from server.";
        }

        // Render the exact real-time text bubble directly into the HTML scroll view
        reply.innerHTML += `
            <div class="aiBubble">
                ${aiTextResponse}
            </div>
        `;
        
        reply.scrollTop = reply.scrollHeight;

    } catch (err) {
        const typingIndicator = document.getElementById("typing");
        if(typingIndicator) typingIndicator.remove();

        reply.innerHTML += `
            <div class="aiBubble" style="border-left: 3px solid #ef4444;">
                Sorry, I am having trouble processing that right now. Please ensure your backend is active.
            </div>
        `;
        reply.scrollTop = reply.scrollHeight;
    }
}

// ⚡ MODIFIED KEYDOWN LISTENER: Captures Enter keys bulletproof across all modern browsers
document.getElementById("message").addEventListener("keydown", function(e){
    if(e.key === "Enter"){
        e.preventDefault(); // Stop the form element from flashing or causing navigation glitches
        send();
    }
});