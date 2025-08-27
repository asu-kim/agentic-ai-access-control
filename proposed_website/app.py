from __future__ import annotations
import os, secrets, sqlite3, json
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional, Tuple

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, g, flash, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet

# ----------------------------------------------------------------------------
# App factory
# ----------------------------------------------------------------------------
def create_app(test_config: Optional[dict] = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DATABASE"] = os.path.join(app.root_path, "app.db")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False  # True behind HTTPS
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=10)  # idle timeout

    VAULT_KEY = os.environ.get("VAULT_KEY")
    if not VAULT_KEY:
        # Demo only: generate ephemeral key (tokens become invalid after restart)
        VAULT_KEY = Fernet.generate_key().decode("utf-8")
    app.config["VAULT_KEY"] = VAULT_KEY

    if test_config:
        app.config.update(test_config)

    # ---------------- DB helpers ----------------
    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(e=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db():
        db = get_db()
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                blob BLOB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                steps TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        db.commit()

    # ---------------- CSRF ----------------
    def get_csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def verify_csrf():
        form_token = request.form.get("csrf_token", "")
        if not form_token or form_token != session.get("csrf_token"):
            abort(400, description="Invalid CSRF token.")

    # ---------------- Login guard ----------------
    def login_required(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if not session.get("user_id"):
                flash("Login required.", "warning")
                return redirect(url_for("login", next=request.path))
            return view(**kwargs)
        return wrapped_view

    # ---------------- Security headers ----------------
    @app.after_request
    def set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-XSS-Protection", "0")
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        return resp

    # ---------------- Vault helpers ----------------
    def get_fernet() -> Fernet:
        return Fernet(app.config["VAULT_KEY"].encode("utf-8"))

    def vault_store(user_id: int, data: dict) -> str:
        f = get_fernet()
        blob = f.encrypt(json.dumps(data).encode("utf-8"))
        token = secrets.token_urlsafe(16)
        db = get_db()
        db.execute("INSERT INTO vault (user_id, token, blob) VALUES (?, ?, ?)", (user_id, token, blob))
        db.commit()
        return token

    def vault_get_blob_by_token(user_id: int, token: str) -> Optional[dict]:
        db = get_db()
        row = db.execute("SELECT blob FROM vault WHERE user_id = ? AND token = ?", (user_id, token)).fetchone()
        if not row:
            return None
        f = get_fernet()
        try:
            return json.loads(f.decrypt(row["blob"]).decode("utf-8"))
        except Exception:
            return None

    def get_remaining_seconds() -> int:
        if not session.get("user_id"):
            return 0
        last_seen_iso = session.get("last_seen")
        try:
            last_seen = datetime.fromisoformat(last_seen_iso) if last_seen_iso else None
        except Exception:
            last_seen = None
        if not last_seen:
            return 0
        elapsed = datetime.now(timezone.utc) - last_seen
        total = timedelta(minutes=10)
        remaining = int(max(0, (total - elapsed).total_seconds()))
        return remaining

    # ---------------- Idle timeout ----------------
    @app.before_request
    def check_idle_timeout():
        uid = session.get("user_id")
        if not uid:
            return
        now = datetime.now(timezone.utc)
        last_seen_iso = session.get("last_seen")
        try:
            last_seen = datetime.fromisoformat(last_seen_iso) if last_seen_iso else None
        except Exception:
            last_seen = None
        if last_seen and (now - last_seen) > timedelta(minutes=10):
            session.clear()
            flash("Session expired after 10 minutes of inactivity. Please sign in again.", "info")
            return redirect(url_for("login"))
        # Do not treat heartbeat/static as activity (avoid resetting idle timer)
        skip_update = request.endpoint in {"session_remaining", "static"}
        if not skip_update:
            session["last_seen"] = now.isoformat()

    # ---------------- Routes ----------------
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            verify_csrf()
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            errors = []
            if not (3 <= len(username) <= 32):
                errors.append("Username must be 3–32 chars.")
            if "@" not in email or len(email) > 255:
                errors.append("Enter a valid email.")
            if len(password) < 8:
                errors.append("Password must be at least 8 chars.")
            if errors:
                for e in errors: flash(e, "danger")
                return render_template("register.html", csrf_token=get_csrf_token())
            db = get_db()
            try:
                db.execute("INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                           (username, email, generate_password_hash(password)))
                db.commit()
            except sqlite3.IntegrityError as e:
                msg = str(e).lower()
                if "users.username" in msg: flash("Username already exists.", "danger")
                elif "users.email" in msg: flash("Email already registered.", "danger")
                else: flash("Registration error.", "danger")
                return render_template("register.html", csrf_token=get_csrf_token())
            flash("Registration complete. Please sign in.", "success")
            return redirect(url_for("login"))
        return render_template("register.html", csrf_token=get_csrf_token())

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            verify_csrf()
            login_id = request.form.get("login_id", "").strip()
            password = request.form.get("password", "")
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = ? OR email = ?",
                              (login_id, login_id)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session.permanent = True
                session["last_seen"] = datetime.now(timezone.utc).isoformat()
                flash("Signed in successfully.", "success")
                next_url = request.args.get("next") or url_for("dashboard")
                return redirect(next_url)
            flash("Sign-in failed: check your username/email and password.", "danger")
            return render_template("login.html", csrf_token=get_csrf_token())
        return render_template("login.html", csrf_token=get_csrf_token())

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.", "info")
        return redirect(url_for("index"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", username=session.get("username"))

    @app.route("/complete", methods=["POST"])
    @login_required
    def complete():
        verify_csrf()
        session.clear()
        flash("Task marked as completed. You have been signed out.", "success")
        return redirect(url_for("index"))

    @app.route("/vault", methods=["GET", "POST"])
    @login_required
    def vault():
        if request.method == "POST":
            verify_csrf()
            cc_name = request.form.get("cc_name", "").strip()
            cc_number = request.form.get("cc_number", "").strip()
            cc_cvv = request.form.get("cc_cvv", "").strip()
            addr_line1 = request.form.get("addr_line1", "").strip()
            addr_city = request.form.get("addr_city", "").strip()
            addr_zip = request.form.get("addr_zip", "").strip()
            if not (cc_name and cc_number and cc_cvv and addr_line1 and addr_city and addr_zip):
                flash("Please fill all fields.", "danger")
                return render_template("vault.html", csrf_token=get_csrf_token())
            token = vault_store(session["user_id"], {
                "cc_name": cc_name,
                "cc_number": cc_number,
                "cc_cvv": cc_cvv,
                "addr_line1": addr_line1,
                "addr_city": addr_city,
                "addr_zip": addr_zip,
            })
            flash("Sensitive data stored securely. Token generated.", "success")
            return redirect(url_for("vault_view", token=token))
        return render_template("vault.html", csrf_token=get_csrf_token())

    @app.route("/vault/<token>")
    @login_required
    def vault_view(token):
        data = vault_get_blob_by_token(session["user_id"], token)
        if not data: abort(404)
        masked = {
            "cc_name": data["cc_name"],
            "cc_number": ("*" * (len(data["cc_number"]) - 4)) + data["cc_number"][-4:],
            "addr_line1": data["addr_line1"][:2] + "***",
            "addr_city": data["addr_city"],
            "addr_zip": "***" + data["addr_zip"][-2:],
        }
        return render_template("vault_view.html", token=token, masked=masked)

    @app.route("/session-remaining")
    @login_required
    def session_remaining():
        return {"seconds": get_remaining_seconds()}

    @app.route("/agent/book-hotel", methods=["POST"])
    @login_required
    def agent_book_hotel():
        verify_csrf()
        db = get_db()
        row = db.execute("SELECT token FROM vault WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                         (session["user_id"],)).fetchone()
        steps = []
        if not row:
            flash("No vaulted payment method found. Add one in Vault first.", "warning")
            return redirect(url_for("vault"))
        token = row["token"]

        steps.append("Agent: Search hotels under $200 in target city/date.")
        steps.append("Agent: Select candidate hotel 'Demo Inn' ($179/night).")
        steps.append("Agent: Request payment authorization via token.")
        amount = 179
        ok, message = mock_charge(session["user_id"], token, amount)
        steps.append(f"Payment Gateway: {message}")
        status = "success" if ok else "failed"

        db.execute("INSERT INTO workflows (user_id, name, steps, status) VALUES (?, ?, ?, ?)",
                   (session["user_id"], "BookHotelUnder200", json.dumps(steps), status))
        db.commit()

        if ok:
            flash("Booking confirmed! (mock)", "success")
        else:
            flash(f"Booking failed: {message}", "danger")
        return redirect(url_for("workflow_history"))

    def mock_charge(user_id: int, token: str, amount: int) -> Tuple[bool, str]:
        if amount > 200:
            return False, "Amount exceeds $200 limit."
        data = vault_get_blob_by_token(user_id, token)
        if not data:
            return False, "Invalid token."
        return True, f"Approved ${amount} using vaulted token ending with {data['cc_number'][-4:]}."

    @app.route("/workflows")
    @login_required
    def workflow_history():
        db = get_db()
        rows = db.execute("SELECT * FROM workflows WHERE user_id = ? ORDER BY id DESC",
                          (session['user_id'],)).fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "steps": json.loads(r["steps"]),
                "created_at": r["created_at"],
            })
        return render_template("workflows.html", items=items)

    # Initialize DB on first run
    with app.app_context():
        init_db()

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": get_csrf_token()}

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
