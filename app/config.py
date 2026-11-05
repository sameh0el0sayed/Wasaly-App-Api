from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    redis_url: str = ""  # e.g. redis://redis:6379/0 ; empty = no Redis (everything works from PostgreSQL)
    location_ttl_seconds: int = 120  # latest driver location in Redis
    location_db_flush_seconds: int = 15  # PostgreSQL gets the latest location at most this often per driver (when Redis is up)
    passkey_rp_id: str = "phoenixsystems.online"  # PASSKEY_RP_ID
    passkey_origins: str = "https://phoenixsystems.online"  # PASSKEY_ORIGINS, comma list (+ android:apk-key-hash:<base64url sha256>)
    db_pool_size: int = 5  # per process: keep (workers x (pool_size + max_overflow)) within the PostgreSQL budget
    db_max_overflow: int = 3
    db_statement_timeout_ms: int = 30000
    secret_key: str = "dev-secret-change-me"
    fernet_key: str = ""  # empty -> derived from secret_key (dev only)
    access_token_minutes: int = 720
    upload_dir: str = "./storage"
    storage_backend: str = "cloudinary"  # cloudinary (default, nothing stored on the server) | local (dev/tests only)
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    cloudinary_folder: str = "wasaly"
    cloudinary_url: str = ""  # alternative: cloudinary://API_KEY:API_SECRET@CLOUD_NAME
    max_upload_mb: int = 8
    offer_ttl_seconds: int = 10  # business rule: fixed 10 seconds
    cors_origins: list[str] = ["*"]
    first_admin_phone: str = "+97300000000"
    first_admin_password: str = "ChangeMe!12345"
    first_admin_name: str = "Super Admin"
    run_background_tasks: bool = True
    app_env: str = "development"  # development | production (production refuses demo seed and default secrets)
    first_admin_must_change_password: bool = True
    # --- demo seed (development only): every value comes from .env, nothing is hard-coded ---
    seed_demo_data: bool = False
    seed_admin_phone: str = ""
    seed_admin_password: str = ""
    seed_admin_name: str = "Admin"
    seed_area_name: str = "Manama"
    seed_area_points: str = ""  # JSON: [[lat,lng],...]
    seed_customer_name: str = ""
    seed_customer_phone: str = ""
    seed_customer_cpr: str = ""
    seed_customer_password: str = ""
    seed_driver_name: str = ""
    seed_driver_phone: str = ""
    seed_driver_cpr: str = ""
    seed_driver_password: str = ""
    seed_driver_license: str = ""
    seed_driver_license_expiry: str = "2099-01-01"
    seed_driver_vehicle: str = ""  # JSON: {"make":..,"model":..,"year":..,"color":..,"plate_number":..,"passenger_capacity":..,"has_ac":true}
    seed_driver_payout_iban: str = ""
    seed_driver_gender: str = ""  # MALE | FEMALE
    seed_airport_name: str = ""
    seed_airport_boundary: str = ""  # JSON, e.g. {"type":"circle","center":[lat,lng],"radius_m":2500}
    seed_airport_fee: str = "0"
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    timezone: str = "Asia/Bahrain"
    public_base_url: str = "http://localhost:8000"  # used for Tap redirect/webhook URLs
    firebase_credentials_json: str = ""  # alternative for hosting platforms: the whole service-account JSON as an env value
    firebase_credentials: str = "secrets/firebase-service-account.json"  # path to the Firebase service-account JSON (FCM push). Empty = push disabled
    tap_secret_key: str = ""  # sk_test_... / sk_live_... from your Tap account
    tap_base_url: str = "https://api.tap.company/v2"
    payout_provider: str = "none"  # simulated | none | http  (see services/payouts.py); "none" = admin transfers manually and marks it done
    payout_http_url: str = ""
    payout_http_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_timeout_seconds: float = 30.0
    gemini_thinking_level: str = "low"  # Gemini 3 models: low | medium | high
    chatbot_rate_limit: int = 10  # requests per user per minute
    ai_service_phone: str = ""
    ai_service_password: str = ""
    ai_service_base_url: str = "http://127.0.0.1:8000"
    ai_review_interval_seconds: int = 900
    ai_review_batch_size: int = 20


settings = Settings()
