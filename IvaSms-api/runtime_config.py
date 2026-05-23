import os


def load_dotenv_file(file_path=".env"):
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except Exception:
        pass


load_dotenv_file()


def _default_state_dir():
    base = (
        os.getenv("IVASMS_STATE_DIR")
        or os.getenv("LOCALAPPDATA")
        or os.getenv("APPDATA")
        or os.getcwd()
    )
    return os.path.join(base, "ivasms-bot")


def _default_webhook_url():
    explicit = os.getenv("BOT_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit

    railway_static = os.getenv("RAILWAY_STATIC_URL", "").strip()
    if railway_static:
        if railway_static.startswith("http://") or railway_static.startswith("https://"):
            return railway_static
        return f"https://{railway_static}"

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}"

    return ""


BOT_TOKEN = os.getenv("BOT_TOKEN", "8875960302:AAGycBgMCA7ZvjrHfeEiAA8IHnCAheS-Qx8")
OTP_CHANNEL_ID = int(os.getenv("OTP_CHANNEL_ID", "-1003971219421"))
OTP_CHANNEL_URL = os.getenv("OTP_CHANNEL_URL", "https://t.me/koskayy")
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "https://t.me/nokoskaybot")

ADMIN_IDS = [8596623218]

IVASMS_EMAIL = os.getenv("IVASMS_EMAIL", "")
IVASMS_PASSWORD = os.getenv("IVASMS_PASSWORD", "")
IVASMS_STATE_DIR = _default_state_dir()
IVASMS_CREDENTIALS_FILE = os.getenv(
    "IVASMS_CREDENTIALS_FILE",
    os.path.join(IVASMS_STATE_DIR, "ivasms_credentials.json"),
)
IVASMS_COOKIES_FILE = os.getenv(
    "IVASMS_COOKIES_FILE",
    os.path.join(IVASMS_STATE_DIR, "cookies.json"),
)

SERVICES = {
    "whatsapp": {
        "name": "WhatsApp",
        "emoji": "💬",
        "icon": "📱",
    },
}

COUNTRY_FLAGS = {
    "ISRAEL": "🇮🇱",
    "AFGHANISTAN": "🇦🇫",
    "UNITED KINGDOM": "🇬🇧",
    "JAPAN": "🇯🇵",
    "GERMANY": "🇩🇪",
    "FRANCE": "🇫🇷",
    "SOUTH KOREA": "🇰🇷",
    "UNITED STATES": "🇺🇸",
    "RUSSIA": "🇷🇺",
    "CHINA": "🇨🇳",
    "BRAZIL": "🇧🇷",
    "INDIA": "🇮🇳",
    "INDONESIA": "🇮🇩",
    "TURKEY": "🇹🇷",
    "ITALY": "🇮🇹",
    "SPAIN": "🇪🇸",
    "AUSTRALIA": "🇦🇺",
    "CANADA": "🇨🇦",
    "MEXICO": "🇲🇽",
    "NETHERLANDS": "🇳🇱",
    "NEPAL": "🇳🇵",
    "ZAMBIA": "🇿🇲",
    "ETHIOPIA": "🇪🇹",
    "SUDAN": "🇸🇩",
    "VENEZUELA": "🇻🇪",
    "CAMEROON": "🇨🇲",
    "SENEGAL": "🇸🇳",
}

NUMBER_HOLD_MINUTES = 20
MAX_ACTIVE_NUMBERS = 3

MONGODB_URI = os.getenv(
    "IVASMS_MONGODB_URI",
    "mongodb+srv://wanzofc:ZW1iXjP47Ug5oMFq@wanzofc.zpki8mr.mongodb.net/koskay?appName=wanzofc",
).strip()
MONGODB_DB_NAME = os.getenv("IVASMS_MONGODB_DB_NAME", "koskay").strip()
PRIVATE_DNS_PRIMARY = os.getenv("IVASMS_PRIVATE_DNS_PRIMARY", "1.1.1.1").strip()
PRIVATE_DNS_SECONDARY = os.getenv("IVASMS_PRIVATE_DNS_SECONDARY", "1.0.0.1").strip()
_DB_BACKEND_ENV = os.getenv("IVASMS_DB_BACKEND", "").strip().lower()
DB_BACKEND = _DB_BACKEND_ENV or ("mongodb" if MONGODB_URI else "mysql")
DB_PATH = os.getenv("IVASMS_DB_PATH", os.path.join(IVASMS_STATE_DIR, "ivasms.db"))
LEGACY_SQLITE_DB_PATH = os.getenv("IVASMS_LEGACY_SQLITE_DB_PATH", DB_PATH)
MYSQL_HOST = os.getenv("IVASMS_DB_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("IVASMS_DB_PORT", "3306"))
MYSQL_DATABASE = os.getenv("IVASMS_DB_NAME", "ivasms_bot")
MYSQL_USER = os.getenv("IVASMS_DB_USER", "ivasms_app")
MYSQL_PASSWORD = os.getenv("IVASMS_DB_PASSWORD", "Ivasms#2026")

OTP_POLL_SECONDS = int(os.getenv("OTP_POLL_SECONDS", "1"))
IVASMS_SESSION_RESTORE_COOLDOWN = int(
    os.getenv("IVASMS_SESSION_RESTORE_COOLDOWN", "30")
)
BOT_RUN_MODE = os.getenv("BOT_RUN_MODE", "polling").strip().lower()
BOT_WEBHOOK_URL = _default_webhook_url()
BOT_WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "").strip()
BOT_LISTEN_HOST = os.getenv("BOT_LISTEN_HOST", "0.0.0.0").strip()
BOT_LISTEN_PORT = int(os.getenv("PORT", os.getenv("BOT_LISTEN_PORT", "8080")))
