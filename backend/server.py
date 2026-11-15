#!/usr/bin/env python3
"""
GutGraph Dispensary - Full-Stack Backend Server
================================================
Architecture:
- High-Performance FastAPI Application
- Concurrency-Hardened SQLite with Write-Ahead Logging (WAL) and 10s busy timeout
- Cryptographic Authentication (Salted PBKDF2-HMAC-SHA256, 100k rounds)
- Case-Insensitive Unique Constraints on Usernames and Emails
- Two-Factor Style Email OTP Dispatch & Verification
- Comprehensive Medical Profile & Gut Health Data Store
- LLM Long-Term Memory Context Engine for the 5 AI Council Agents
- Indian GST Invoice Calculation & Order Storage (CGST 2.5%, SGST 2.5%, Packaging)
- Built-in Static File Server for Frontend & Assets
"""

import os
import re
import sys
import time
import json
import secrets
import hashlib
import sqlite3
import random
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mixology_bridge import analyze_meal_text
from env_loader import load_dotenv
from demo_data import MEALS
from agent import llm as agent_llm
from agent.personas import roster, AGENT_BY_HANDLE
from agent.pipeline import run_pipeline
from agent import foodo_tools, tool_loop
import foodoscope

load_dotenv()

# Auth configuration (real email OTP + JWT). JWT_SECRET falls back to a random
# per-process secret so the demo runs without a .env; set it for stable sessions.
JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_hex(32)
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()

# Base Directory Paths
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")
INDEX_PATH = os.path.join(FRONTEND_DIR, "index.html")
DB_PATH = os.path.join(BACKEND_DIR, "gutgraph.db")

app = FastAPI(
    title="GutGraph Dispensary Production API",
    description="Full-stack backend providing Auth, Medical Profile Store, LLM Long-term Memory, and GST Checkout",
    version="2.0.0"
)

# Enable CORS for local and web requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Database Connection & Setup -----------------
@contextmanager
def get_db():
    """
    Thread-safe SQLite connection with WAL journal mode and busy timeout
    to resolve simultaneous logins, registrations, and chats flawlessly.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            email TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            medical_history TEXT DEFAULT '',
            current_situation TEXT DEFAULT '',
            allergies TEXT DEFAULT '',
            health_goals TEXT DEFAULT '',
            additional_notes TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS otp_codes (
            email TEXT PRIMARY KEY COLLATE NOCASE,
            code TEXT NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS chat_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            bot_id TEXT NOT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            items_json TEXT NOT NULL,
            subtotal REAL NOT NULL,
            cgst REAL NOT NULL,
            sgst REAL NOT NULL,
            packaging REAL NOT NULL,
            grand_total REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_memory(user_id);
        """)
    print(f"[GutGraph DB] Initialized database at {DB_PATH}")

# Call init_db on server load
init_db()

# ----------------- Cryptographic Utilities & Rate Limiting -----------------
def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """bcrypt (per the new auth spec). The salt column is now unused and kept only
    for backwards compatibility with pre-bcrypt rows."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"), ""

def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """bcrypt for new rows, legacy PBKDF2 for rows written before the switch."""
    if expected_hash.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), expected_hash.encode("utf-8"))
        except ValueError:
            return False
    # legacy PBKDF2-HMAC-SHA256 (100k rounds) for existing accounts
    if not salt:
        return False
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()
    return secrets.compare_digest(hashed, expected_hash)

def generate_token(user_id: int) -> str:
    """JWT access token (HS256), 7-day expiry, matching the new auth spec."""
    return jwt.encode(
        {"id": str(user_id), "exp": datetime.now(timezone.utc) + timedelta(days=7)},
        JWT_SECRET,
        algorithm="HS256",
    )

# In-memory rate limiting against brute force attacks
FAILED_ATTEMPTS: Dict[str, List[float]] = {}
RATE_LIMIT_WINDOW = 300  # 5 minutes
MAX_FAILED_ATTEMPTS = 5

def check_rate_limit(key: str):
    now = time.time()
    attempts = [t for t in FAILED_ATTEMPTS.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    FAILED_ATTEMPTS[key] = attempts
    if len(attempts) >= MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Please wait 5 minutes before retrying."
        )

def record_failed_attempt(key: str):
    now = time.time()
    attempts = [t for t in FAILED_ATTEMPTS.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    attempts.append(now)
    FAILED_ATTEMPTS[key] = attempts

def clear_failed_attempts(key: str):
    if key in FAILED_ATTEMPTS:
        del FAILED_ATTEMPTS[key]

def get_current_user_id(authorization: Optional[str] = Header(None)) -> Optional[int]:
    """Validate a Bearer JWT and return the user id, or None."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        payload = jwt.decode(parts[1], JWT_SECRET, algorithms=["HS256"])
        return int(payload["id"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None

def require_current_user(authorization: Optional[str] = Header(None)) -> int:
    user_id = get_current_user_id(authorization)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in."
        )
    return user_id

# ----------------- Request / Response Models -----------------
class SendOtpRequest(BaseModel):
    email: str

class VerifyOtpRequest(BaseModel):
    email: str
    otp: str

class RegisterRequest(BaseModel):
    email: str
    otp: str
    username: str
    password: str

class LoginRequest(BaseModel):
    login: str  # Can be username or email
    password: str

class ProfileUpdateRequest(BaseModel):
    medical_history: Optional[str] = ""
    current_situation: Optional[str] = ""
    allergies: Optional[str] = ""
    health_goals: Optional[str] = ""
    additional_notes: Optional[str] = ""

class ChatRequest(BaseModel):
    message: str
    bot_id: Optional[str] = "all"

class CheckoutItem(BaseModel):
    id: str
    name: str
    qty: int
    price: float = 120.0

class MealAnalyzeRequest(BaseModel):
    meal_text: str = Field(..., min_length=1, max_length=2000)
    demo_mode: bool = True


class ParliamentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class CheckoutRequest(BaseModel):
    items: List[CheckoutItem]

# ----------------- Whimsical Random Credential Generator -----------------
ADJECTIVES = [
    "Bio", "Microbe", "Gut", "Kombucha", "Lacto", "Ferment",
    "Polyphenol", "Zesty", "Botanical", "Active", "Bifido",
    "Flora", "Prebiotic", "Akkermansia", "Herbal", "Crisp", "Vital"
]
NOUNS = [
    "Scout", "Hero", "Guardian", "Alchemist", "Ninja", "Forager",
    "Sentry", "Warrior", "Captain", "Curator", "Seeker", "Sage",
    "Knight", "Doctor", "Pioneer", "Master"
]

@app.get("/api/auth/random-credentials")
def generate_random_credentials():
    """Generates a unique gut-themed handle and a strong cryptographic password."""
    for _ in range(50):
        adj = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        num = random.randint(10, 999)
        candidate = f"{adj}{noun}{num}"
        with get_db() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone()
            if not exists:
                username = candidate
                break
    else:
        username = f"GutHero{secrets.token_hex(3)}"

    words = ["Gut", "Bio", "Shield", "Flora", "Digest", "Pure", "Lacto"]
    symbols = ["!", "@", "#", "$", "%", "*"]
    p_word = random.choice(words)
    p_sym = random.choice(symbols)
    p_num = random.randint(100, 999)
    p_suffix = secrets.token_hex(2)
    password = f"{p_word}{p_sym}{p_num}{p_suffix}"

    return {
        "username": username,
        "password": password
    }

# ----------------- Username Uniqueness Check -----------------
@app.get("/api/auth/check-username")
def check_username(username: str = Query(..., min_length=2, max_length=30)):
    clean_username = username.strip()
    if not re.match(r"^[a-zA-Z0-9_-]{3,30}$", clean_username):
        return {
            "available": False,
            "message": "Username must be 3-30 letters, numbers, hyphens or underscores."
        }
    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (clean_username,)).fetchone()
        if row:
            return {
                "available": False,
                "message": f"'{clean_username}' is already taken. Try another or generate one!"
            }
        return {
            "available": True,
            "message": f"'{clean_username}' is available!"
        }

# ----------------- Real email OTP delivery (with dev fallback) -----------------
def send_email(receiver: str, otp: str) -> None:
    """Send the OTP via Gmail SMTP. Raises on failure; only called when
    EMAIL_USER / EMAIL_PASS are configured."""
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER and EMAIL_PASS are not configured.")
    message = EmailMessage()
    message["Subject"] = "GUTGRAPH Email Verification"
    message["From"] = EMAIL_USER
    message["To"] = receiver
    message.set_content(
        f"Welcome to GUTGRAPH!\n\nYour email verification OTP is: {otp}\n\n"
        f"This OTP expires in 10 minutes. If you did not request it, ignore this email."
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(message)


# ----------------- Auth: Step 1 - Send OTP -----------------
@app.post("/api/auth/send-otp")
def send_otp(req: SendOtpRequest, request: Request):
    email = req.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"otp:{client_ip}")

    otp = f"{random.randint(100000, 999999)}"
    expires_at = time.time() + 600  # 10 minutes expiry

    with get_db() as conn:
        conn.execute("""
            INSERT INTO otp_codes (email, code, expires_at, attempts)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(email) DO UPDATE SET
                code = excluded.code,
                expires_at = excluded.expires_at,
                attempts = 0
        """, (email, otp, expires_at))

    email_sent = False
    if EMAIL_USER and EMAIL_PASS:
        try:
            send_email(email, otp)
            email_sent = True
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("OTP email failed for %s: %s", email, exc)

    if not email_sent:
        print(f"\n==========================================")
        print(f" [GutGraph Security] OTP FOR {email}: {otp}")
        print(f" (Valid for 10 minutes)")
        print(f"==========================================\n")

    return {
        "success": True,
        "message": f"6-digit verification code dispatched to {email}."
                   + (" Check your inbox." if email_sent else ""),
        **({"demo_otp": otp} if not email_sent else {}),
    }

# ----------------- Auth: Step 2 - Verify OTP -----------------
@app.post("/api/auth/verify-otp")
def verify_otp(req: VerifyOtpRequest, request: Request):
    email = req.email.strip().lower()
    otp = req.otp.strip()

    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"verify:{client_ip}")

    with get_db() as conn:
        row = conn.execute("SELECT code, expires_at, attempts FROM otp_codes WHERE email = ?", (email,)).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="No verification code requested for this email.")
        if time.time() > row["expires_at"]:
            raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new one.")
        if row["attempts"] >= 5:
            raise HTTPException(status_code=429, detail="Too many invalid attempts. Please request a new code.")
        if row["code"] != otp:
            conn.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
            record_failed_attempt(f"verify:{client_ip}")
            raise HTTPException(status_code=400, detail="Invalid verification code. Please check and re-enter.")

    clear_failed_attempts(f"verify:{client_ip}")
    return {
        "success": True,
        "verified": True,
        "message": "Email verified successfully! Please set up your handle and password."
    }

# ----------------- Auth: Step 3 - Register -----------------
@app.post("/api/auth/register")
def register(req: RegisterRequest, request: Request):
    email = req.email.strip().lower()
    otp = req.otp.strip()
    username = req.username.strip()
    password = req.password

    if not re.match(r"^[a-zA-Z0-9_-]{3,30}$", username):
        raise HTTPException(status_code=400, detail="Username must be 3-30 characters (letters, numbers, underscores).")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"register:{client_ip}")

    with get_db() as conn:
        otp_row = conn.execute("SELECT code, expires_at FROM otp_codes WHERE email = ?", (email,)).fetchone()
        if not otp_row or otp_row["code"] != otp or time.time() > otp_row["expires_at"]:
            raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

        if conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Username '{username}' is already in use. Please pick another.")
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists. Please log in.")

        pw_hash, salt = hash_password(password)

        cursor = conn.execute(
            "INSERT INTO users (username, email, password_hash, salt) VALUES (?, ?, ?, ?)",
            (username, email, pw_hash, salt)
        )
        user_id = cursor.lastrowid
        conn.execute("INSERT INTO profiles (user_id) VALUES (?)", (user_id,))
        conn.execute("DELETE FROM otp_codes WHERE email = ?", (email,))

        access_token = generate_token(user_id)

    clear_failed_attempts(f"register:{client_ip}")
    return {
        "success": True,
        "token": access_token,
        "user": {
            "id": user_id,
            "username": username,
            "email": email
        },
        "message": f"Welcome to GutGraph, {username}! Please configure your gut profile."
    }

# ----------------- Auth: Login -----------------
@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    login_id = req.login.strip()
    password = req.password

    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"login:{client_ip}")

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username, email, password_hash, salt FROM users WHERE username = ? OR email = ?",
            (login_id, login_id)
        ).fetchone()

        if not user or not verify_password(password, user["salt"], user["password_hash"]):
            record_failed_attempt(f"login:{client_ip}")
            raise HTTPException(status_code=401, detail="Invalid username/email or password.")

        access_token = generate_token(user["id"])

        profile = conn.execute(
            "SELECT medical_history, current_situation, allergies, health_goals, additional_notes FROM profiles WHERE user_id = ?",
            (user["id"],)
        ).fetchone()

    clear_failed_attempts(f"login:{client_ip}")
    return {
        "success": True,
        "token": access_token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"]
        },
        "profile": dict(profile) if profile else {},
        "message": f"Welcome back, {user['username']}! Long-term memory loaded."
    }

# ----------------- Auth: Logout -----------------
@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None)):
    # JWT is stateless: the client discards the token. Kept as an explicit
    # endpoint for the frontend to call.
    return {"success": True, "message": "Logged out successfully. Remove the token on the frontend."}

# ----------------- Auth: Me / Current Status -----------------
@app.get("/api/auth/me")
def get_me(authorization: Optional[str] = Header(None)):
    user_id = get_current_user_id(authorization)
    if not user_id:
        return {"authenticated": False, "user": None, "profile": None}

    with get_db() as conn:
        user = conn.execute("SELECT id, username, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        profile = conn.execute(
            "SELECT medical_history, current_situation, allergies, health_goals, additional_notes FROM profiles WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    return {
        "authenticated": True,
        "user": dict(user),
        "profile": dict(profile) if profile else {}
    }

# ----------------- Medical Profile Store -----------------
@app.get("/api/profile")
def get_profile(user_id: int = Depends(require_current_user)):
    with get_db() as conn:
        profile = conn.execute(
            "SELECT medical_history, current_situation, allergies, health_goals, additional_notes, updated_at FROM profiles WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if not profile:
            return {}
        return dict(profile)

@app.post("/api/profile")
def update_profile(req: ProfileUpdateRequest, user_id: int = Depends(require_current_user)):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO profiles (user_id, medical_history, current_situation, allergies, health_goals, additional_notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                medical_history = excluded.medical_history,
                current_situation = excluded.current_situation,
                allergies = excluded.allergies,
                health_goals = excluded.health_goals,
                additional_notes = excluded.additional_notes,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id,
            req.medical_history or "",
            req.current_situation or "",
            req.allergies or "",
            req.health_goals or "",
            req.additional_notes or ""
        ))

    return {
        "success": True,
        "message": "Health profile & LLM long-term memory synchronized successfully!"
    }

# ----------------- LLM Chat with Long-Term Memory -----------------
@app.get("/api/chat/history")
def get_chat_history(user_id: int = Depends(require_current_user)):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sender, bot_id, author, text, timestamp FROM chat_memory WHERE user_id = ? ORDER BY id ASC LIMIT 100",
            (user_id,)
        ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["timestamp"] = _utc_to_ist(r["timestamp"])
        return out


# Per-agent personas for the side chat: routing stays, the words come from the LLM.
_PERSONA = [
    ("rough", "F. prausnitzii · Fibre Forager", "prebiotic fibre, short-chain fatty acids and daily gut motility",
     ["fibre", "fiber", "dal", "salad", "oats", "bloat", "gas", "stool", "constipat"],
     "Inulin, beta-glucans, and resistant starches feed Faecalibacterium prausnitzii to synthesize short-chain fatty acids (SCFAs)."),
    ("spike", "C. albicans · Sugar Sentry", "blood-sugar response and zero-sugar drinks",
     ["sugar", "coke", "cola", "sweet", "spike", "glucose", "insulin", "diabetes", "energy"],
     "Free liquid sugars cause sharp glycemic excursions. GutGraph cans are brewed with zero cane sugar and under 35 kcal."),
    ("rack", "B. thetaiotaomicron · Spice Parliament", "spices, polyphenols and reflux-friendly choices",
     ["spice", "turmeric", "pepper", "ginger", "curcumin", "reflux", "acidity", "gerd", "burn"],
     "Piperine in Tellicherry black pepper enhances curcumin bioavailability by up to 2000%."),
    ("dahi", "L. bulgaricus · Ferment Scout", "live cultures, ferments and dairy sensitivity",
     ["curd", "chaas", "dahi", "probiotic", "kefir", "ferment", "lactose", "milk", "dairy"],
     "Live wild strains colonize the mucosal lumen, lowering local pH to deter pathogenic blooms."),
]
_DEFAULT_PERSONA = (
    "kavach", "A. muciniphila · Lining Guard",
    "the gut lining, polyphenols and overall gut comfort",
    "Maintaining mucosal tight-junction integrity requires plant polyphenols and low endotoxin stress.",
)


def _pick_persona(low: str):
    for bot_id, author, specialty, keys, fallback in _PERSONA:
        if any(k in low for k in keys):
            return {"bot_id": bot_id, "author": author, "specialty": specialty, "fallback": fallback}
    return {"bot_id": _DEFAULT_PERSONA[0], "author": _DEFAULT_PERSONA[1],
            "specialty": _DEFAULT_PERSONA[2], "fallback": _DEFAULT_PERSONA[3]}


def _template_line(fallback: str, profile_context: str = "") -> str:
    """The canned answer, still honouring the saved profile (the whole point of the
    memory feature) instead of a generic line that ignores it."""
    line = fallback
    if profile_context:
        line += (f" Your saved profile says — {profile_context} — so weigh that "
                 "before you pick a can.")
    return line


async def _llm_reply(author, specialty, profile_context, user_name, message, fallback, history=None):
    """Word the side-chat answer with the LLM in the chosen agent's voice.

    When a provider is configured the turn runs through the tool loop, so the agent can
    read RecipeDB / FlavorDB for a specific dish or ingredient before answering. The
    canned line still answers when nothing is reachable, so the drawer never goes silent.
    Returns {"text", "calls", "source"}.
    """
    system = (
        f"You are {author}, one of GutGraph's five gut-council agents. Your specialty is "
        f"{specialty}. Answer the user's question about food, gut health or which GutGraph "
        f"botanical drink pairs with their meal. Be short (under 80 words), specific and "
        f"friendly, in character. Never diagnose or prescribe medicine; if it sounds medical, "
        f"say you are not a doctor and suggest seeing one."
    )
    if tool_loop.enabled():
        system += foodo_tools.TOOL_PROMPT
    if profile_context:
        system += f" The user's saved health profile: {profile_context}. Use it silently, never repeat it back unless asked."
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    if tool_loop.enabled():
        res = await tool_loop.run(messages, max_tokens=400)
        if res["source"] == "live" and res["text"]:
            return {"text": res["text"], "calls": res["calls"], "source": "live"}
        return {"text": _template_line(fallback, profile_context),
                "calls": res["calls"], "source": "template"}
    try:
        return {"text": await agent_llm.complete(messages, max_tokens=320),
                "calls": [], "source": "live"}
    except agent_llm.LLMError:
        return {"text": _template_line(fallback, profile_context),
                "calls": [], "source": "template"}


@app.post("/api/chat")
async def process_chat(req: ChatRequest, authorization: str = Header(None)):
    user_id = get_current_user_id(authorization)
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    profile_context = ""
    user_name = "Guest"

    if user_id:
        with get_db() as conn:
            u = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            if u:
                user_name = u["username"]
            p = conn.execute("SELECT medical_history, current_situation, allergies, health_goals, additional_notes FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
            if p:
                parts = []
                if p["medical_history"]:
                    parts.append(f"Medical History: {p['medical_history']}")
                if p["allergies"]:
                    parts.append(f"Allergies/Sensitivities: {p['allergies']}")
                if p["current_situation"]:
                    parts.append(f"Current Situation: {p['current_situation']}")
                if p["health_goals"]:
                    parts.append(f"Health Goals: {p['health_goals']}")
                if parts:
                    profile_context = " | ".join(parts)

    history = []
    if user_id:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT sender, text FROM chat_memory WHERE user_id = ? ORDER BY id DESC LIMIT 8",
                (user_id,),
            ).fetchall()
        for r in reversed(rows):
            history.append({"role": "assistant" if r["sender"] == "agent" else "user", "content": r["text"]})

    low = message.lower()

    if any(q in low for q in ["who am i", "my profile", "what do you know", "my memory", "remember me"]):
        selected_bot, author = "kavach", "A. muciniphila · Memory Guardian"
        tools_used, voice = [], "memory"
        if user_id and profile_context:
            reply = f"Hello @{user_name}! In your synchronized Long-Term Memory, I have: {profile_context}. All 5 council agents check this before every answer!"
        elif user_id:
            reply = f"Hello @{user_name}! Your account is active, but your health profile is blank. Head to the Account menu to add medical history and allergies!"
        else:
            reply = "You are currently browsing as a Guest! Sign up or log in from the top-right button to enable Long-Term Memory."
    else:
        persona = _pick_persona(low)
        selected_bot, author = persona["bot_id"], persona["author"]
        out = await _llm_reply(author, persona["specialty"], profile_context, user_name,
                              message, persona["fallback"], history)
        reply, tools_used, voice = out["text"], out["calls"], out["source"]

    if user_id:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO chat_memory (user_id, sender, bot_id, author, text) VALUES (?, 'user', 'user', ?, ?)",
                (user_id, user_name, message)
            )
            conn.execute(
                "INSERT INTO chat_memory (user_id, sender, bot_id, author, text) VALUES (?, 'agent', ?, ?, ?)",
                (user_id, selected_bot, author, reply)
            )

    return {
        "sender": "agent",
        "bot_id": selected_bot,
        "author": author,
        "text": reply,
        "long_term_memory": bool(user_id),
        "user": user_name,
        "source": voice,
        "tools_used": [{"tool": c.get("tool"), "args": c.get("args"),
                        "source": c.get("source"), "ok": c.get("ok"),
                        "offline": c.get("offline"), "ms": c.get("ms"),
                        "error": c.get("error")}
                       for c in (tools_used or [])],
    }

# ----------------- Meal Lab (5-agent gut council) -----------------
@app.post("/api/analyze-meal")
def api_analyze_meal(req: MealAnalyzeRequest):
    """Powers the Meal Lab in the Dispensary SPA (server-side agent cards)."""
    return analyze_meal_text(req.meal_text)


# ----------------- FoodoScope (RecipeDB / FlavorDB) -----------------
_DEMO_PAIRS = {
    "turmeric": [
        {"name": "mango", "similarity": 0.388, "shared_molecules": 103},
        {"name": "ginger", "similarity": 0.385, "shared_molecules": 89},
        {"name": "black pepper", "similarity": 0.210, "shared_molecules": 45},
    ],
    "ginger": [
        {"name": "turmeric", "similarity": 0.385, "shared_molecules": 89},
        {"name": "lemon", "similarity": 0.156, "shared_molecules": 34},
    ],
    "mango": [{"name": "turmeric", "similarity": 0.388, "shared_molecules": 103}],
}


@app.get("/foodoscope/status")
def foodoscope_status():
    return {
        "configured": foodoscope.enabled(),
        "base_url": foodoscope._base() if foodoscope.enabled() else None,
        "reachable": foodoscope.reachable(),
        "cocktaildb_enabled": foodoscope.cocktail_enabled(),
        "cocktaildb_policy": ("18+ gated by event policy — set FOODOSCOPE_COCKTAILDB=1 "
                              "only if the team's shortlist says CLEARED"),
    }


@app.get("/foodoscope/pair")
def foodoscope_pair(ingredient: str, limit: int = 8):
    """FlavorDB pairing for an ingredient; curated demo table when off-campus.

    Live rows carry FlavorDB's own `reported_similar` AND our recomputed
    intersection/Jaccard over both entities' molecule sets — see
    foodoscope.pairings() for why the two numbers differ.
    """
    key = ingredient.strip().lower()
    live = foodoscope.pairings(key)
    if live:
        return {"ingredient": ingredient, "source": "flavordb", "pairs": live[:limit]}
    demo = _DEMO_PAIRS.get(key, [])
    if not demo:
        for alias, rows in _DEMO_PAIRS.items():
            if alias and (alias in key or key in alias):
                demo = rows
                break
    return {"ingredient": ingredient, "source": "demo", "pairs": demo}


@app.get("/foodoscope/recipe-search")
def foodoscope_recipe(title: str, limit: int = 5):
    hits = foodoscope.recipe_search(title, limit=limit)
    if hits is None:
        raise HTTPException(
            status_code=503,
            detail="FoodoScope unreachable (campus network only) or no API key configured.",
        )
    return {"title": title, "source": "recipedb", "results": hits}


class ResolveMealRequest(BaseModel):
    meal_text: str = Field(..., min_length=1, max_length=2000)


# Words that are not dishes: connectives, meal words, and quality adjectives. A
# single-word fallback on "instant" once matched a South African milk tart, so the
# adjectives are excluded from queries entirely.
_QUERY_STOP = {
    "with", "and", "plus", "some", "the", "for", "of", "my", "our", "had", "ate",
    "eating", "today", "tonight", "plate", "bowl", "was", "were", "a", "an",
    "instant", "fresh", "quick", "easy", "simple", "basic", "plain", "homemade",
    "best", "perfect", "tasty", "delicious", "spicy", "sweet", "cold", "hot",
    "warm", "extra", "little", "full", "half", "big", "small", "dinner", "lunch",
    "breakfast", "snack", "meal", "food", "drink", "drinks", "side",
}


def _candidate_queries(text: str):
    """RecipeDB by-title is a literal title search, so "palak paneer with roti"
    finds nothing. Try the meaningful words first, longest to shortest."""
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower())
             if len(w) > 2 and w not in _QUERY_STOP]
    cands = []
    for n in (4, 3, 2):
        if len(words) >= n:
            cands.append(" ".join(words[:n]))
    cands.append(text.strip())
    cands.append(" ".join(sorted(words)))
    seen, out = set(), []
    for c in cands:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def _best_recipe(rows, words):
    """Pick the RecipeDB hit whose title best overlaps the query words."""
    best, best_score = None, -1
    for r in rows or []:
        title = (r.get("name") or "").lower()
        score = sum(1 for w in words if w in title)
        if score > best_score:
            best, best_score = r, score
    return best


@app.post("/api/resolve-meal")
def api_resolve_meal(req: ResolveMealRequest):
    """Meal Lab, RecipeDB-first: resolve the typed dish to a real recipe, then score
    its ACTUAL ingredient list with our rule engine.

    Falls back to scoring the raw text (source="keyword") whenever the gateway is
    unreachable, so the meal lab never depends on the campus network.
    """
    text = req.meal_text.strip()
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower())
             if len(w) > 2 and w not in _QUERY_STOP]
    hits, used_query = None, None
    for q in _candidate_queries(text):
        hits = foodoscope.recipe_search(q, limit=5)
        if hits:
            used_query = q
            break
    # Deliberately NO single-word fallback. RecipeDB by-title is a literal title
    # search, so a loose one-word query returns confidently wrong dishes — measured
    # live: "instant" -> South African Instant Milk Tart, "roti" -> Algerian Roast
    # Chicken (roti = roast in French). Scoring the wrong recipe is worse than
    # scoring the user's own words, so a miss falls through to the keyword path.
    best = _best_recipe(hits, words)
    detail = foodoscope.recipe_detail(best["id"]) if best and best.get("id") else None
    if detail and detail.get("ingredients"):
        names = [i["name"] for i in detail["ingredients"]]
        analysis = analyze_meal_text(" ".join(names))
        return {
            "source": "recipedb",
            "query_used": used_query,
            "recipe": {k: detail.get(k) for k in (
                "id", "name", "region", "sub_region", "continent",
                "calories", "minutes", "servings", "nutrition", "diet")},
            "ingredients": names,
            "steps": detail.get("steps"),
            "scored_text": " ".join(names),
            **analysis,
        }
    # Say WHY we fell back: "RecipeDB has no recipe for this" reads very differently
    # to a judge than "RecipeDB is unreachable".
    lookup = "unreachable" if not foodoscope.reachable() else "no_match"
    return {"source": "keyword", "lookup": lookup, "recipe": None,
            "ingredients": [], **analyze_meal_text(text)}


@app.get("/api/foodoscope/{path:path}")
def foodoscope_proxy(path: str, request: Request):
    """Browser-safe passthrough to the FoodoScope gateway — the key stays server-side.

    Allow-listed to RecipeDB + FlavorDB; /cocktaildb is refused unless the team is
    cleared (foodoscope.cocktail_enabled). Everything is disk-cached by
    foodoscope._get, so a repeated call is free.
    """
    clean = "/" + path.lstrip("/")
    if clean.startswith(foodoscope.COCKTAIL_PREFIX):
        if not foodoscope.cocktail_enabled():
            raise HTTPException(
                status_code=403,
                detail="CocktailDB is 18+ gated by event policy and disabled in this build.",
            )
    elif not clean.startswith(tuple(foodoscope.PROXY_ALLOWED)):
        raise HTTPException(
            status_code=400,
            detail=f"Path not allowed. Use one of: {', '.join(foodoscope.PROXY_ALLOWED)}",
        )
    if not foodoscope.enabled():
        raise HTTPException(status_code=503, detail="FoodoScope not configured (no API key).")
    params = dict(request.query_params)
    data = foodoscope._get(clean, params)
    if data is None:
        raise HTTPException(
            status_code=503,
            detail="FoodoScope unreachable (campus network only) or that path returned no data.",
        )
    return {"path": clean, "params": params, "source": "foodoscope", "data": data}


# ----------------- Agent Parliament (Grok-style SSE) -----------------
@app.get("/agents")
def list_parliament_agents():
    info = agent_llm.provider_info()
    return {
        "mode": info["mode"],
        "provider": info["provider"],
        "model": info["model"],
        "agents": roster(),
    }


class AgentDMRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: Optional[List[Dict[str, str]]] = None


@app.post("/api/agents/{handle}/chat")
async def agent_private_chat(handle: str, req: AgentDMRequest, authorization: Optional[str] = Header(None)):
    """Private 1:1 window with ONE named council agent — no keyword routing and no
    other agent's voice. Words come from the LLM; the persona template answers
    when the model is unreachable, so the window never goes silent."""
    agent = AGENT_BY_HANDLE.get(handle.strip().upper())
    if not agent:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{handle}'.")

    system = (
        agent["system"]
        + (foodo_tools.TOOL_PROMPT if tool_loop.enabled() else "")
        + " You are now in a PRIVATE one-to-one window with the user: the other agents cannot hear you. "
          "Answer questions about food, gut health and which GutGraph botanical drink pairs with their meal, "
          "in under 80 words, in character. Never diagnose or prescribe medicine; if it sounds medical, say "
          "you are not a doctor and suggest seeing one."
    )

    user_id = get_current_user_id(authorization)
    if user_id:
        with get_db() as conn:
            p = conn.execute(
                "SELECT medical_history, current_situation, allergies, health_goals FROM profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if p:
                parts = [
                    f"{label}: {p[col]}"
                    for col, label in (("medical_history", "Medical History"),
                                       ("current_situation", "Current Situation"),
                                       ("allergies", "Allergies/Sensitivities"),
                                       ("health_goals", "Health Goals"))
                    if p[col]
                ]
                if parts:
                    system += (" The user's saved health profile (" + " | ".join(parts) +
                               ") — use it silently, never repeat it back unless asked.")

    messages = [{"role": "system", "content": system}]
    for turn in (req.history or [])[-8:]:
        role = "user" if str(turn.get("role", "user")).lower() == "user" else "assistant"
        content = str(turn.get("content") or "").strip()[:1200]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": req.message.strip()})

    tools_used = []
    source = "live"
    if tool_loop.enabled():
        res = await tool_loop.run(messages, max_tokens=320)
        tools_used = res["calls"]
        if res["source"] == "live" and res["text"]:
            text = res["text"]
        else:
            text = agent.get("dm_line") or f"I'm {agent['name']} ({agent['role']}). Ask me what feeds me."
            source = "template"
    else:
        try:
            text = await agent_llm.complete(messages, max_tokens=320)
        except agent_llm.LLMError:
            text = agent.get("dm_line") or f"I'm {agent['name']} ({agent['role']}). Ask me what feeds me."
            source = "template"

    return {"handle": agent["handle"], "name": agent["name"], "role": agent["role"],
            "text": text, "source": source,
            "tools_used": [{"tool": c.get("tool"), "args": c.get("args"),
                            "source": c.get("source"), "ok": c.get("ok"),
                            "offline": c.get("offline"), "ms": c.get("ms"),
                            "error": c.get("error")}
                           for c in tools_used]}


@app.get("/meals")
def list_demo_meals():
    return [
        {"id": m["id"], "name": m["name"], "emoji": m["emoji"]}
        for m in MEALS
    ]


@app.post("/chat/stream")
async def parliament_chat_stream(req: ParliamentChatRequest):
    async def event_source():
        async for ev in run_pipeline(req.message):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ----------------- Checkout & Bill Generation API -----------------
@app.post("/api/checkout")
def checkout(req: CheckoutRequest, authorization: Optional[str] = Header(None)):
    user_id = get_current_user_id(authorization)
    items = req.items
    if not items:
        raise HTTPException(status_code=400, detail="Cannot generate bill for an empty stash.")

    subtotal = sum(item.qty * item.price for item in items)
    cgst = round(subtotal * 0.025, 2)
    sgst = round(subtotal * 0.025, 2)
    packaging = 0.0 if subtotal >= 360.0 else 25.0
    grand_total = round(subtotal + cgst + sgst + packaging, 2)

    order_id = f"GG-INV-{int(time.time())}-{random.randint(100, 999)}"

    with get_db() as conn:
        conn.execute("""
            INSERT INTO orders (order_id, user_id, items_json, subtotal, cgst, sgst, packaging, grand_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id,
            user_id,
            json.dumps([item.dict() for item in items]),
            subtotal,
            cgst,
            sgst,
            packaging,
            grand_total
        ))

    return {
        "success": True,
        "order_id": order_id,
        "customer": f"User #{user_id}" if user_id else "Guest Patron",
        "subtotal": subtotal,
        "cgst": cgst,
        "sgst": sgst,
        "total_tax": round(cgst + sgst, 2),
        "packaging": packaging,
        "grand_total": grand_total,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "APPROVED_PAID"
    }

# ----------------- Static File & Asset Routes -----------------
if os.path.exists(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/agent-room.css")
def serve_agent_room_css():
    path = os.path.join(FRONTEND_DIR, "agent-room.css")
    if os.path.isfile(path):
        return FileResponse(path, media_type="text/css")
    raise HTTPException(status_code=404, detail="agent-room.css missing")


@app.get("/agent-room.js")
def serve_agent_room_js():
    path = os.path.join(FRONTEND_DIR, "agent-room.js")
    if os.path.isfile(path):
        return FileResponse(path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="agent-room.js missing")


# ================= User Meal History · Custom Drink Builder · Gut Dashboard =================

def _init_history_tables() -> None:
    """V2 tables for per-user meal/drink history and the dashboard."""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meal_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                meal_text TEXT NOT NULL,
                match_id TEXT NOT NULL DEFAULT '',
                agents_json TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS drink_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                meal_id INTEGER,
                drink_name TEXT NOT NULL,
                recipe_json TEXT NOT NULL DEFAULT '{}',
                ingredients_json TEXT NOT NULL DEFAULT '[]',
                taken INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_meal_user ON meal_logs(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_drink_user ON drink_logs(user_id, created_at);
            """
        )


_init_history_tables()


def _utc_to_ist(ts: str) -> str:
    """SQLite CURRENT_TIMESTAMP is UTC 'YYYY-MM-DD HH:MM:SS' -> IST (UTC+05:30)."""
    if not ts:
        return ts
    try:
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)


# ---- Ingredient + drink catalogs (offline fallback; FlavorDB on campus) ----

DRINK_INGREDIENTS: Dict[str, Dict[str, str]] = {
    "mint": {"name": "Fresh mint", "benefit": "cooling · digestive", "note": "menthol soothes the lining"},
    "ginger": {"name": "Fresh ginger", "benefit": "anti-nausea · warming", "note": "gingerol pairs with curcumin"},
    "lemon": {"name": "Lemon / lime", "benefit": "vitamin C · brightens", "note": "citral lifts polyphenol absorption"},
    "turmeric": {"name": "Turmeric (haldi)", "benefit": "anti-inflammatory", "note": "curcumin + piperine = far better absorption"},
    "black_pepper": {"name": "Black pepper", "benefit": "absorption booster", "note": "piperine unlocks curcumin"},
    "mango": {"name": "Mango / aam", "benefit": "fibre · sweetness", "note": "shares 103 aroma molecules with turmeric (Jaccard 0.388)"},
    "amla": {"name": "Amla", "benefit": "vitamin C · polyphenols", "note": "exceptionally polyphenol-dense"},
    "hibiscus": {"name": "Hibiscus / roselle", "benefit": "anthocyanins", "note": "deep colour = anthocyanin load"},
    "kokum": {"name": "Kokum", "benefit": "cooling · digestif", "note": "traditional cooling digestif"},
    "fennel": {"name": "Fennel (saunf)", "benefit": "carminative", "note": "soothes bloating"},
    "cardamom": {"name": "Cardamom (elaichi)", "benefit": "aromatic · carminative", "note": "cineole adds lift"},
    "honey": {"name": "Honey", "benefit": "sweetness · prebiotic", "note": "natural prebiotic"},
    "curd": {"name": "Curd / yogurt", "benefit": "live cultures", "note": "adds Lactobacillus"},
    "rock_salt": {"name": "Rock salt (sendha)", "benefit": "electrolytes", "note": "mineral finish"},
}

BASE_DRINKS: Dict[str, Dict[str, Any]] = {
    "desi-cool": {"name": "Desi Cool", "base": ["curd", "mint", "rock_salt"], "why": "repopulate live cultures after a culture-light plate"},
    "pink-paradise": {"name": "Pink Paradise", "base": ["hibiscus", "lemon"], "why": "give fibre fermenters a polyphenol fuel load"},
    "mint-condition": {"name": "Mint Condition", "base": ["mint", "lemon", "honey"], "why": "cool the sugar spike with menthol and citral"},
    "golden-hour": {"name": "Golden Hour", "base": ["turmeric", "black_pepper", "ginger"], "why": "broaden the polyphenol parliament"},
    "sunkissed": {"name": "Sunkissed", "base": ["amla", "honey", "ginger"], "why": "rebuild the Akkermansia barrier shield"},
    "ruby-rush": {"name": "Ruby Rush", "base": ["kokum", "fennel", "rock_salt"], "why": "balanced, cooling finish"},
}

_OPTIONAL_BOOSTS = ["mint", "ginger", "lemon", "amla", "hibiscus", "mango", "fennel", "cardamom"]


def _ing(ing_id: str) -> Optional[Dict[str, str]]:
    return DRINK_INGREDIENTS.get(ing_id)


def _build_drink_questions(match_id: str) -> List[Dict[str, Any]]:
    base = BASE_DRINKS.get(match_id, BASE_DRINKS["ruby-rush"])
    qs: List[Dict[str, Any]] = []
    for ing_id in base["base"]:
        ing = _ing(ing_id)
        if ing:
            qs.append({"id": ing_id, "question": f"Do you have {ing['name'].lower()} available?",
                       "options": ["Yes", "No"], "role": "base"})
    for ing_id in _OPTIONAL_BOOSTS:
        if ing_id in base["base"]:
            continue
        if len(qs) >= 6:
            break
        ing = _ing(ing_id)
        if ing:
            qs.append({"id": ing_id, "question": f"Optional boost — do you have {ing['name'].lower()}?",
                       "options": ["Yes", "No"], "role": "boost"})
    return qs


def _assemble_drink(match_id: str, answers: Dict[str, str]) -> Dict[str, Any]:
    base = BASE_DRINKS.get(match_id, BASE_DRINKS["ruby-rush"])
    chosen = list(base["base"])
    added: List[str] = []
    for aid, ans in (answers or {}).items():
        if str(ans).strip().lower() in ("yes", "y", "true", "1") and aid in DRINK_INGREDIENTS and aid not in chosen:
            chosen.append(aid)
            added.append(aid)
    ingredients: List[Dict[str, Any]] = []
    for c in chosen:
        ing = _ing(c)
        if ing:
            ingredients.append({"id": c, **ing})
    steps = [f"Muddle or blend {', '.join(i['name'].lower() for i in ingredients[:3])}."]
    if any(i["id"] == "curd" for i in ingredients):
        steps.append("Whisk the curd in last and keep it cool so the live cultures survive.")
    steps.append("Top with chilled water or ice, a pinch of rock salt, and serve immediately.")
    if added:
        steps.append("Your extra picks upgrade the base — enjoy the custom build.")
    name = base["name"] + " · Custom" if added else base["name"]
    return {
        "name": name,
        "base": base["name"],
        "why": base["why"],
        "ingredients": ingredients,
        "added": added,
        "steps": steps,
    }


class MealLogRequest(BaseModel):
    meal_text: str


class DrinkSuggestRequest(BaseModel):
    meal_text: str


class DrinkBuildRequest(BaseModel):
    match_id: str
    answers: Dict[str, str] = {}


class DrinkLogRequest(BaseModel):
    meal_id: Optional[int] = None
    drink_name: str
    recipe_json: Dict[str, Any] = {}
    ingredients_json: List[str] = []


@app.post("/api/meals")
def log_meal(req: MealLogRequest, user_id: int = Depends(require_current_user)):
    text = (req.meal_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Meal text is required.")
    analysis = analyze_meal_text(text)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO meal_logs (user_id, meal_text, match_id, agents_json) VALUES (?, ?, ?, ?)",
            (user_id, text, analysis.get("match_id", ""), json.dumps(analysis.get("agents", []))),
        )
        meal_id = cur.lastrowid
    return {
        "id": meal_id, "meal_text": text, "match_id": analysis.get("match_id", ""),
        "agents": analysis.get("agents", []), "safety_warning": analysis.get("safety_warning", False),
    }


@app.get("/api/history")
def get_history(user_id: int = Depends(require_current_user)):
    with get_db() as conn:
        meals = conn.execute(
            "SELECT id, meal_text, match_id, created_at FROM meal_logs WHERE user_id = ? ORDER BY id DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        drinks = conn.execute(
            "SELECT id, meal_id, drink_name, taken, created_at FROM drink_logs WHERE user_id = ? ORDER BY id DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        chats = conn.execute(
            "SELECT sender, author, text, timestamp FROM chat_memory WHERE user_id = ? ORDER BY id DESC LIMIT 60",
            (user_id,),
        ).fetchall()
    events: List[Dict[str, Any]] = []
    for m in meals:
        events.append({"type": "meal", "id": m["id"], "meal_text": m["meal_text"],
                       "match_id": m["match_id"], "created_at": _utc_to_ist(m["created_at"])})
    for d in drinks:
        events.append({"type": "drink", "id": d["id"], "meal_id": d["meal_id"],
                       "drink_name": d["drink_name"], "taken": bool(d["taken"]), "created_at": _utc_to_ist(d["created_at"])})
    for c in chats:
        events.append({"type": "chat", "sender": c["sender"], "author": c["author"],
                       "text": c["text"], "created_at": _utc_to_ist(c["timestamp"])})
    events.sort(key=lambda e: e["created_at"], reverse=True)
    return {"events": events, "meals_count": len(meals), "drinks_count": len(drinks)}


@app.post("/api/drink/suggest")
def suggest_drink(req: DrinkSuggestRequest, user_id: int = Depends(require_current_user)):
    text = (req.meal_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Meal text is required.")
    analysis = analyze_meal_text(text)
    match_id = analysis.get("match_id", "ruby-rush")
    base = BASE_DRINKS.get(match_id, BASE_DRINKS["ruby-rush"])
    weakest = min(analysis["agents"], key=lambda a: a["score"])["name"] if analysis.get("agents") else None
    return {
        "match_id": match_id,
        "base_drink": {
            "name": base["name"],
            "ingredients": [_ing(i)["name"] for i in base["base"] if _ing(i)],
            "why": base["why"],
        },
        "questions": _build_drink_questions(match_id),
        "weakest_agent": weakest,
    }


@app.post("/api/drink/build")
def build_drink(req: DrinkBuildRequest, user_id: int = Depends(require_current_user)):
    match_id = req.match_id if req.match_id in BASE_DRINKS else "ruby-rush"
    return _assemble_drink(match_id, req.answers or {})


@app.post("/api/drink/log")
def log_drink(req: DrinkLogRequest, user_id: int = Depends(require_current_user)):
    name = (req.drink_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Drink name is required.")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO drink_logs (user_id, meal_id, drink_name, recipe_json, ingredients_json, taken) VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, req.meal_id, name, json.dumps(req.recipe_json or {}), json.dumps(req.ingredients_json or [])),
        )
        drink_id = cur.lastrowid
    return {"id": drink_id, "drink_name": name, "logged": True}


def _metric(value: int, key: str, name: str, color: str, desc: str) -> Dict[str, Any]:
    return {"key": key, "name": name, "value": value, "color": color, "desc": desc}


@app.get("/api/dashboard")
def get_dashboard(user_id: int = Depends(require_current_user)):
    with get_db() as conn:
        meals = conn.execute(
            "SELECT agents_json FROM meal_logs WHERE user_id = ? ORDER BY id ASC", (user_id,)
        ).fetchall()
        _row = conn.execute(
            "SELECT COUNT(*) AS c FROM drink_logs WHERE user_id = ? AND taken = 1", (user_id,)
        ).fetchone()
        drinks_taken = _row["c"] if _row else 0
    if not meals:
        return {
            "metrics": [
                _metric(0, "alpha", "Alpha Diversity", "#4CAF50", "Species-richness proxy — log a meal to seed it."),
                _metric(0, "beta", "Beta Diversity", "#3B82F6", "How much your plate varies between meals."),
                _metric(0, "gmhi", "Gut Microbiome Health Index (GMHI)", "#F59E0B", "Composite gut-health score."),
                _metric(0, "enbi", "Ecological Network Balance Index (ENBI)", "#8B5CF6", "Fermented vs. refined balance."),
            ],
            "meals_count": 0, "drinks_count": 0,
            "note": "No meals logged yet — log tonight's plate to start tracking.",
        }
    sums = {"dahi": 0.0, "rough": 0.0, "spike": 0.0, "rack": 0.0, "kavach": 0.0}
    n = 0
    for m in meals:
        try:
            agents = json.loads(m["agents_json"] or "[]")
        except Exception:
            continue
        for a in agents:
            if a.get("id") in sums:
                sums[a["id"]] += float(a.get("score", 0))
        n += 1
    n = max(1, n)
    avg = {k: v / n for k, v in sums.items()}
    drink_bonus = min(8.0, drinks_taken * 4.0)
    alpha = round(max(5, min(98, 0.34 * avg["rough"] + 0.34 * avg["dahi"] + 0.32 * avg["rack"] + drink_bonus * 0.3)))
    beta = round(max(5, min(98, 18 + 6 * min(n, 12) + 0.25 * avg["rack"])))
    gmhi = round(max(5, min(98, 0.30 * avg["rough"] + 0.25 * avg["dahi"] + 0.25 * avg["spike"] + 0.20 * avg["kavach"] + drink_bonus * 0.5)))
    enbi = round(max(5, min(98, 0.50 * avg["dahi"] + 0.50 * avg["spike"])))
    return {
        "metrics": [
            _metric(alpha, "alpha", "Alpha Diversity", "#4CAF50", "Species richness, estimated from fibre + ferments + spice variety."),
            _metric(beta, "beta", "Beta Diversity", "#3B82F6", "How much your plate varies between meals."),
            _metric(gmhi, "gmhi", "Gut Microbiome Health Index (GMHI)", "#F59E0B", "Composite of fibre, live cultures, glycemic load and barrier."),
            _metric(enbi, "enbi", "Ecological Network Balance Index (ENBI)", "#8B5CF6", "Balance between fermented foods and refined/sugary ones."),
        ],
        "meals_count": len(meals), "drinks_count": drinks_taken,
        "averages": {k: round(v, 1) for k, v in avg.items()},
        "note": "Illustrative estimates computed from your logged meals and drinks — not a lab test.",
    }


# ----------------- Weekly Targets (mode slider) + Activity Calendar -----------------

TARGETS: Dict[str, Dict[str, int]] = {
    "easy": {"meals": 3, "drinks": 2, "chats": 3},
    "medium": {"meals": 5, "drinks": 3, "chats": 5},
    "hard": {"meals": 7, "drinks": 5, "chats": 7},
}


def _init_targets_table() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS weekly_targets (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'medium',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


_init_targets_table()


def _get_mode(user_id: int) -> str:
    with get_db() as conn:
        r = conn.execute("SELECT mode FROM weekly_targets WHERE user_id = ?", (user_id,)).fetchone()
    return r["mode"] if r else "medium"


def _bucket_ist(rows):
    c: Dict[str, int] = {}
    for r in rows:
        d = _utc_to_ist(r["created_at"])[:10]
        c[d] = c.get(d, 0) + 1
    return c


class TargetRequest(BaseModel):
    mode: str


@app.get("/api/targets")
def get_targets(user_id: int = Depends(require_current_user)):
    mode = _get_mode(user_id)
    return {"mode": mode, "targets": TARGETS.get(mode, TARGETS["medium"]), "modes": ["easy", "medium", "hard"]}


@app.post("/api/targets")
def set_targets(req: TargetRequest, user_id: int = Depends(require_current_user)):
    mode = (req.mode or "").strip().lower()
    if mode not in TARGETS:
        raise HTTPException(status_code=400, detail="mode must be easy, medium or hard")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO weekly_targets (user_id, mode) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, updated_at = CURRENT_TIMESTAMP",
            (user_id, mode),
        )
    return {"mode": mode, "targets": TARGETS[mode]}


@app.get("/api/calendar")
def get_calendar(user_id: int = Depends(require_current_user)):
    mode = _get_mode(user_id)
    targets = TARGETS.get(mode, TARGETS["medium"])
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    monday = (now_ist - timedelta(days=now_ist.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = (monday.astimezone(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        meals = conn.execute("SELECT created_at FROM meal_logs WHERE user_id = ? AND created_at >= ?", (user_id, cutoff)).fetchall()
        drinks = conn.execute("SELECT created_at FROM drink_logs WHERE user_id = ? AND created_at >= ?", (user_id, cutoff)).fetchall()
        chats = conn.execute("SELECT timestamp AS created_at FROM chat_memory WHERE user_id = ? AND timestamp >= ?", (user_id, cutoff)).fetchall()
    mc = _bucket_ist(meals)
    dc = _bucket_ist(drinks)
    cc = _bucket_ist(chats)
    days = []
    totals = {"meals": 0, "drinks": 0, "chats": 0}
    today_str = now_ist.strftime("%Y-%m-%d")
    for i in range(7):
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        m, dr, ch = mc.get(ds, 0), dc.get(ds, 0), cc.get(ds, 0)
        totals["meals"] += m
        totals["drinks"] += dr
        totals["chats"] += ch
        days.append({"date": ds, "label": d.strftime("%a"), "day": d.day, "month": d.strftime("%b"),
                     "meals": m, "drinks": dr, "chats": ch, "total": m + dr + ch, "today": ds == today_str})
    return {"mode": mode, "targets": targets, "days": days, "totals": totals,
            "week_start": monday.strftime("%d %b"), "week_end": (monday + timedelta(days=6)).strftime("%d %b")}


# ----------------- Personalised Drink Recommendations (Netflix-style) -----------------

REC_DRINKS = [
    {"id": "desi-cool", "name": "Desi Cool", "tag": "Cultured masala chaas & roasted jeera", "color": "#8BD3A6", "emoji": "🥛", "kcal": 22, "fibre": 1.2, "agent": "dahi", "why": "Covers your live-culture (ferment) gap."},
    {"id": "pink-paradise", "name": "Pink Paradise", "tag": "Wild guava & aloe spritz", "color": "#FF8DB8", "emoji": "🩷", "kcal": 30, "fibre": 4.1, "agent": "rough", "why": "The highest fibre in the range — feeds your fibre fermenters."},
    {"id": "mint-condition", "name": "Mint Condition", "tag": "Spearmint, lime & micro ginger", "color": "#43E0C2", "emoji": "🍃", "kcal": 19, "fibre": 2.5, "agent": "spike", "why": "The lightest one (19 kcal) — cuts through sweet, heavy plates."},
    {"id": "golden-hour", "name": "Golden Hour", "tag": "Haldi, tellicherry pepper & ginger", "color": "#F2C12E", "emoji": "🌅", "kcal": 28, "fibre": 2.8, "agent": "rack", "why": "Broadens your polyphenol spectrum with turmeric + pepper."},
    {"id": "sunkissed", "name": "Sunkissed", "tag": "Golden mango pulp & wild citrus", "color": "#FF9D36", "emoji": "🌞", "kcal": 34, "fibre": 3.2, "agent": "kavach", "why": "Rebuilds your gut-lining barrier with the most botanicals."},
    {"id": "ruby-rush", "name": "Ruby Rush", "tag": "Wild kokum & pomegranate", "color": "#C21E56", "emoji": "🍒", "kcal": 32, "fibre": 3.0, "agent": None, "why": "The treat can for when your plate is already solid."},
]

_GAP_LABELS = {"dahi": "live cultures", "rough": "fibre", "spike": "sugar control",
               "rack": "spice diversity", "kavach": "gut lining"}

_GOAL_HINTS = {
    "sugar": "mint-condition", "sweet": "mint-condition", "spike": "mint-condition", "energy": "mint-condition",
    "fibre": "pink-paradise", "fiber": "pink-paradise", "weight": "pink-paradise", "bloat": "pink-paradise",
    "gut": "desi-cool", "digest": "desi-cool", "probiotic": "desi-cool", "ferment": "desi-cool",
    "inflamm": "golden-hour", "spice": "golden-hour", "reflux": "mint-condition",
    "skin": "sunkissed", "immun": "sunkissed",
}

# How much each drink answers each of the 5 gut agents (for gap_coverage across all agents).
DRINK_AFFINITY = {
    "desi-cool":      {"dahi": 1.0, "spike": 0.4, "rough": 0.2},
    "pink-paradise":  {"rough": 1.0, "kavach": 0.4, "spike": 0.3},
    "mint-condition": {"spike": 1.0, "rack": 0.3, "rough": 0.2},
    "golden-hour":    {"rack": 1.0, "kavach": 0.5, "dahi": 0.2},
    "sunkissed":      {"kavach": 1.0, "rough": 0.4, "rack": 0.2},
    "ruby-rush":      {"kavach": 0.4, "dahi": 0.4, "spike": 0.3},
}

# Allergen/diet keywords each drink carries (hard filter against the user's profile).
DRINK_ALLERGENS = {
    "desi-cool": ["milk", "dairy", "lactose"],
    "golden-hour": ["coconut", "nut"],
}

# Static popularity prior (stand-in for a 48h event-log popularity).
_POPULARITY = {"desi-cool": 0.8, "mint-condition": 0.9, "golden-hour": 0.7,
               "pink-paradise": 0.6, "sunkissed": 0.5, "ruby-rush": 0.6}



def _demo_analyses() -> List[Dict[str, Any]]:
    """The demo plates, scored by the same rules — the baseline a guest starts from."""
    return [analyze_meal_text(" ".join(i["name"] for i in m["ingredients"])) for m in MEALS]


def _recommendation_engine(user_id: Optional[int],
                          extra_analyses: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The personalised drink ranking, shared by the dashboard endpoint and the story
    acts. Score source, in order: the plates this user logged, then analyses handed in
    by the caller (a guest's session meals), then the demo plates — never a flat 50."""
    meals, logged_names, prof = [], [], None
    if user_id is not None:
        with get_db() as conn:
            meals = conn.execute("SELECT agents_json FROM meal_logs WHERE user_id = ? ORDER BY id ASC", (user_id,)).fetchall()
            logged_names = [r["drink_name"].lower() for r in conn.execute("SELECT drink_name FROM drink_logs WHERE user_id = ?", (user_id,)).fetchall()]
            prof = conn.execute("SELECT allergies, health_goals FROM profiles WHERE user_id = ?", (user_id,)).fetchone()

    analyses: List[Dict[str, Any]] = []
    for m in meals:
        try:
            agents = json.loads(m["agents_json"] or "[]")
        except Exception:
            continue
        if agents:
            analyses.append({"agents": agents})
    source = "logged"
    if not analyses and extra_analyses:
        analyses, source = extra_analyses, "session"
    if not analyses:
        analyses, source = _demo_analyses(), "demo"

    sums = {"dahi": 0.0, "rough": 0.0, "spike": 0.0, "rack": 0.0, "kavach": 0.0}
    n = 0
    for a in analyses:
        for row in (a.get("agents") or []):
            if row.get("id") in sums:
                sums[row["id"]] += float(row.get("score", 0))
        n += 1
    n = max(1, n)
    avg = {k: v / n for k, v in sums.items()}
    allergies_text = (prof["allergies"] or "").lower() if prof else ""
    goals_text = (prof["health_goals"] or "").lower() if prof else ""
    weakest = min(avg.items(), key=lambda kv: kv[1])[0] if n else None

    items, skipped = [], []
    for d in REC_DRINKS:
        # Hard allergy/diet filter — the PDF's "never cut" filter.
        hit_allergen = next((a for a in DRINK_ALLERGENS.get(d["id"], []) if a in allergies_text), None)
        if hit_allergen:
            skipped.append({"id": d["id"], "name": d["name"], "reason": "Filtered — contains " + hit_allergen})
            continue
        # gap_coverage across all 5 agents (not just the weakest)
        gap = sum(((100.0 - avg[ag]) / 100.0) * w for ag, w in DRINK_AFFINITY.get(d["id"], {}).items())
        new_for_you = not any(d["name"].lower() in ln for ln in logged_names)
        goal = sum(1.0 for kw, did in _GOAL_HINTS.items() if did == d["id"] and kw in goals_text)
        score = 1.0 * gap + 0.3 * (1.0 if new_for_you else 0.0) + 0.4 * min(goal, 1.0) + 0.1 * _POPULARITY.get(d["id"], 0.5)
        is_top = (d["agent"] == weakest) if weakest else False
        if is_top and weakest:
            reason = "Top match — your biggest gap is " + _GAP_LABELS.get(weakest, weakest)
        elif goal:
            reason = "Matches your health goal"
        elif new_for_you:
            reason = "New for you"
        else:
            reason = "Tried — still a solid pick"
        items.append({**d, "score": round(score, 3), "new_for_you": new_for_you, "is_top": is_top, "reason": reason})
    items.sort(key=lambda x: (-x["score"], -x["fibre"]))
    return {"items": items, "skipped": skipped, "top_gap": weakest,
            "gap_label": _GAP_LABELS.get(weakest) if weakest else None,
            "meals_logged": n, "source": source,
            "avg": {k: round(v) for k, v in avg.items()}}


@app.get("/api/recommendations")
def get_recommendations(user_id: int = Depends(require_current_user)):
    """Personalised drink ranking for the dashboard (unchanged shape)."""
    return _recommendation_engine(user_id)


# ----------------- Story acts read the same engine -------------------------
# The illustrated can in each act of the story is NOT a fixed list: it is the drink
# this engine ranks highest for that act's dimension, after the same hard allergen
# filter and goal weighting the dashboard uses. Log a plate, set an allergy, or
# drink a can, and the acts change on the next render.
ACT_DIMENSIONS = (
    ("day0", "spike", "day 0 — the sugar problem"),
    ("day3", "dahi", "day 3 — the cultures"),
    ("day7", "rack", "day 7 — the polyphenols"),
    ("day14", "kavach", "day 14 — the finish"),
)


class StoryActsRequest(BaseModel):
    meal_texts: List[str] = Field(default_factory=list, max_length=12)


def _story_acts(user_id: Optional[int], meal_texts: List[str]) -> Dict[str, Any]:
    """Per-act drinks for the story, straight from the recommendation engine.

    Guests hand in the plates they analysed this session, so the acts move as their
    habits change without an account; signed-in users are scored from their logged
    history instead (and their allergy profile can filter a can out).
    """
    extra = [analyze_meal_text(t) for t in (meal_texts or [])[:12] if (t or "").strip()]
    engine = _recommendation_engine(user_id, extra)
    items = engine["items"]
    avg = engine.get("avg") or {}
    weakest = engine.get("top_gap")

    def best_for(dimension: str):
        """Highest-affinity drink for a dimension; engine score breaks ties."""
        ranked = sorted(
            items,
            key=lambda d: (-DRINK_AFFINITY.get(d["id"], {}).get(dimension, 0.0), -d["score"]),
        )
        return ranked[0] if ranked and DRINK_AFFINITY.get(ranked[0]["id"], {}).get(dimension, 0) > 0 else None

    acts = []
    for act, dimension, title in ACT_DIMENSIONS:
        pick = best_for(dimension)
        healthy = (avg.get(dimension, 50) >= 70) and bool(weakest)
        fallback = best_for(str(weakest)) if (healthy and weakest) else None
        chosen = fallback or pick
        if chosen is None:
            continue
        why = (
            f"Your {_GAP_LABELS.get(dimension, dimension)} reads {avg.get(dimension, 50)}/100, "
            f"so this act carries the engine's pick for your real gap — "
            f"{_GAP_LABELS.get(weakest, weakest)}."
            if healthy else
            f"{_GAP_LABELS.get(dimension, dimension).capitalize()} reads "
            f"{avg.get(dimension, 50)}/100 over {engine['meals_logged']} plate(s) — "
            f"the engine ranks this the drink that answers it."
        )
        acts.append({
            "act": act, "title": title, "dimension": dimension,
            "dimension_label": _GAP_LABELS.get(dimension, dimension),
            "score": avg.get(dimension, 50),
            "can": chosen["id"], "can_name": chosen["name"], "can_tag": chosen.get("tag"),
            "can_color": chosen.get("color"), "can_kcal": chosen.get("kcal"),
            "mode": "weakest" if healthy else "theme",
            "reason": chosen.get("reason"), "why": why,
        })
    return {"source": engine.get("source", "engine"), "meals_used": engine["meals_logged"],
            "scores": avg, "weakest": weakest,
            "weakest_label": _GAP_LABELS.get(weakest) if weakest else None,
            "top_pick": items[0]["id"] if items else None,
            "top_pick_name": items[0]["name"] if items else None,
            "skipped": engine["skipped"], "acts": acts}


@app.post("/api/story-acts")
def post_story_acts(req: StoryActsRequest, authorization: Optional[str] = Header(None)):
    """The story's per-act drinks, including the plates analysed in this browser."""
    return _story_acts(get_current_user_id(authorization), req.meal_texts)


@app.get("/api/story-acts")
def get_story_acts(meal: Optional[List[str]] = Query(None),
                   authorization: Optional[str] = Header(None)):
    """Same answers without a body (curl / judges). Repeat ?meal= to pass plates."""
    return _story_acts(get_current_user_id(authorization), meal or [])


@app.get("/")
def serve_index():
    if os.path.exists(INDEX_PATH):
        return FileResponse(INDEX_PATH, media_type="text/html")
    return {"message": "GutGraph Backend Active. Place index.html inside the frontend/ directory."}

@app.get("/index.html")
def serve_index_html():
    return serve_index()

if __name__ == "__main__":
    import uvicorn
    print(f"\n🌿 Launching GutGraph Dispensary Server on http://0.0.0.0:8000 ...")
    print(f"📂 Frontend root: {FRONTEND_DIR}")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
