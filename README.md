Ribat Chatbot API
=================

Description
-----------
Ribat Chatbot API هو Backend API مبني باستخدام FastAPI،
بيقدّم شات بوت متخصص في دعم الأسرة والتربية والتواصل بين الأهل والأبناء،
ومدمج مع Google Gemini AI + Knowledge Base + نظام حجز استشارات (Demo).

Technology Stack
----------------
- Python 3.9+
- FastAPI
- Google Gemini (google-genai)
- Pydantic
- Uvicorn

Main Endpoint
-------------
POST /chat

This endpoint receives user messages and returns:
- AI-generated reply
- Structured cards (tips, specialists, booking slots)

Example Request
---------------
POST /chat
Content-Type: application/json

{
  "user_id": "user_001",
  "messages": [
    {
      "role": "user",
      "content": "ابني مراهق ومش بيتكلم معايا"
    }
  ],
  "child_age": 14
}

Example Response
----------------
{
  "message_id": "msg_xxxxx",
  "reply": "نص إرشادي من البوت...",
  "cards": [
    {
      "type": "tip",
      "title": "نصيحة عملية",
      "body": "ابدئي وقت هدوء..."
    }
  ]
}

Environment Variables
---------------------
- GEMINI_API_KEY : Required only for /chat (optional for running the server)
- RIBAT_ADMIN_KEY : Admin key for KB management (optional)
- GEMINI_MODEL : Default is gemini-2.5-flash (optional)
- RIBAT_DATA_DIR : Default is "data" (optional)

Run Instructions
----------------
1) Install dependencies:
   pip install -r requirements.txt

2) Run the server (local):
   uvicorn ribat_bot_api:app --host 0.0.0.0 --port 8000

3) Swagger UI:
   http://localhost:8000/docs

Production Deploy
-----------------
Start Command:
uvicorn ribat_bot_api:app --host 0.0.0.0 --port $PORT

Health Check:
GET /health

Important Notes
---------------
- The API is strictly limited to family, parenting, and educational topics.
- Programming, medical diagnosis, and medication-related questions are blocked by design.
- Do NOT commit .env file.
- If using persistent JSON files, ignore data files in git (e.g. data/*.json).

Author
------
Fatma
