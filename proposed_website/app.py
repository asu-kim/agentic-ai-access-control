from __future__ import annotations
import os, secrets, sqlite3, json, random, base64, hashlib
from datetime import datetime, timedelta, timezone, date
from functools import wraps
from typing import Optional, Tuple

from flask import Flask, render_template, request, redirect, url_for, session, g, flash, abort
from werkzeug.security import check_password_hash as wz_check_password_hash  # legacy support
from cryptography.fernet import Fernet

try:
    from argon2 import PasswordHasher
    from argon2 import exceptions as argon2_exceptions
    _HAS_ARGON2 = True
    _ph = PasswordHasher(time_cost=2, memory_cost=102400, parallelism=8, hash_len=32, salt_len=16)
except Exception:
    _HAS_ARGON2 = False
    _ph = None


def pbkdf2_hash(password: str, iterations: int = 100_000, salt_bytes: int = 16) -> str:
    """Return 'iterations$salt_b64$hash_b64'"""
    salt = os.urandom(salt_bytes)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def pbkdf2_verify(stored: str, password: str) -> bool:
    """Verify our 'iterations$salt$hash' format."""
    try:
        iterations_str, salt_b64, hash_b64 = stored.split("$")
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64)
        stored_hash = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        # constant-time compare
        return secrets.compare_digest(dk, stored_hash)
    except Exception:
        return False

def looks_like_our_pbkdf2(stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 3: return False
    try:
        int(parts[0])
        base64.b64decode(parts[1])
        base64.b64decode(parts[2])
        return True
    except Exception:
        return False

def create_app(test_config: Optional[dict] = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DATABASE"] = os.path.join(app.root_path, "app.db")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=10)

    VAULT_KEY = os.environ.get("VAULT_KEY") or Fernet.generate_key().decode("utf-8")
    app.config["VAULT_KEY"] = VAULT_KEY
    prev = os.environ.get("PREVIOUS_VAULT_KEYS", "")
    app.config["PREVIOUS_VAULT_KEYS"] = [k.strip() for k in prev.split(",") if k.strip()]

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
        # Lightweight migrations for legacy DBs
        def col_exists(table, col):
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r[1] == col for r in rows)
        def index_exists(table, idx):
            rows = db.execute(f"PRAGMA index_list({table})").fetchall()
            return any(r[1] == idx for r in rows)

        if not col_exists("users", "email"):
            db.execute("ALTER TABLE users ADD COLUMN email TEXT")
            if not index_exists("users", "idx_users_email_unique"):
                db.execute("CREATE UNIQUE INDEX idx_users_email_unique ON users(email)")
        if not index_exists("users", "idx_users_username_unique"):
            try:
                db.execute("CREATE UNIQUE INDEX idx_users_username_unique ON users(username)")
            except Exception:
                pass
        db.commit()

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

    def login_required(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if not session.get("user_id"):
                flash("Login required.", "warning")
                return redirect(url_for("login", next=request.path))
            return view(**kwargs)
        return wrapped_view

    @app.after_request
    def set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-XSS-Protection", "0")
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        return resp

    def get_fernet() -> Fernet:
        return Fernet(app.config["VAULT_KEY"].encode("utf-8"))
    def get_prev_fernets():
        res = []
        for k in app.config.get("PREVIOUS_VAULT_KEYS", []):
            try:
                res.append(Fernet(k.encode("utf-8")))
            except Exception:
                pass
        return res

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
        row = db.execute("SELECT id, blob FROM vault WHERE user_id = ? AND token = ?", (user_id, token)).fetchone()
        if not row:
            return None
        vid = row["id"]
        blob = row["blob"]
        curr = get_fernet()
        try:
            data = json.loads(curr.decrypt(blob).decode("utf-8"))
            return data
        except Exception:
            pass
        for f in get_prev_fernets():
            try:
                data = json.loads(f.decrypt(blob).decode("utf-8"))
                try:
                    new_blob = curr.encrypt(json.dumps(data).encode("utf-8"))
                    db.execute("UPDATE vault SET blob = ? WHERE id = ?", (new_blob, vid))
                    db.commit()
                except Exception:
                    pass
                return data
            except Exception:
                continue
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
        skip_update = request.endpoint in {"session_remaining", "static"}
        if not skip_update:
            session["last_seen"] = now.isoformat()

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
                hashed = pbkdf2_hash(password)  # PBKDF2-HMAC(SHA-256) + random salt
                db.execute("INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                           (username, email, hashed))
                db.commit()
            except sqlite3.IntegrityError as e:
                msg = str(e).lower()
                if "users.username" in msg: flash("Username already exists.", "danger")
                elif "users.email" in msg or "idx_users_email_unique" in msg: flash("Email already registered.", "danger")
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
            ok = False
            if user:
                stored = user["password_hash"] or ""
                # 1) Our PBKDF2 format: iterations$salt$hash
                if looks_like_our_pbkdf2(stored):
                    ok = pbkdf2_verify(stored, password)
                # 2) Werkzeug legacy "pbkdf2:sha256:iters$salt$hash"
                elif stored.startswith("pbkdf2:sha256:"):
                    try:
                        ok = wz_check_password_hash(stored, password)
                        if ok:
                            # migrate to our PBKDF2 format
                            new_hash = pbkdf2_hash(password)
                            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
                            db.commit()
                    except Exception:
                        ok = False
                # 3) Argon2 legacy (if library available)
                elif stored.startswith("$argon2") and _HAS_ARGON2:
                    try:
                        _ph.verify(stored, password)
                        ok = True
                        # migrate to our PBKDF2 format
                        new_hash = pbkdf2_hash(password)
                        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
                        db.commit()
                    except Exception:
                        ok = False
                else:
                    ok = False

            if ok:
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
        if ok: flash("Booking confirmed! (mock)", "success")
        else:  flash(f"Booking failed: {message}", "danger")
        return redirect(url_for("workflow_history"))

    def _ensure_vault_token() -> Optional[str]:
        db = get_db()
        row = db.execute("SELECT token FROM vault WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                         (session["user_id"],)).fetchone()
        return row["token"] if row else None

    def _insert_workflow(name: str, steps: list[str], status: str) -> int:
        db = get_db()
        cur = db.execute(
            "INSERT INTO workflows (user_id, name, steps, status) VALUES (?, ?, ?, ?)",
            (session["user_id"], name, json.dumps(steps), status)
        )
        db.commit()
        return cur.lastrowid

    def _extract_amount_from_steps(steps: list[str]) -> Optional[int]:
        for s in steps:
            if s.startswith("AMOUNT="):
                try: return int(s.split("=",1)[1])
                except Exception: return None
        return None

    @app.route("/scenario/product", methods=["GET", "POST"])
    @login_required
    def scenario_product():
        if request.method == "POST":
            verify_csrf()
            item_name = request.form.get("item_name", "").strip()
            max_price = int(request.form.get("max_price", "0") or 0)
            min_rating = float(request.form.get("min_rating", "0") or 0)
            max_delivery_days = int(request.form.get("max_delivery_days", "0") or 0)
            if not item_name or max_price <= 0:
                flash("Please provide item name and a positive max price.", "danger")
                return render_template("scenario_product.html", csrf_token=get_csrf_token())
            if random.random() < 0.30:
                found = int(max_price * 1.15) or (max_price + 10)
                steps = [
                    f"Agent: Search '{item_name}' with rating ≥ {min_rating} and delivery ≤ {max_delivery_days} days.",
                    f"Agent: Found offers starting at ${found}, which exceeds your max ${max_price}.",
                    "Agent: Declined creating a payable scenario."
                ]
                _insert_workflow("ProductPurchase", steps, "rejected")
                flash("No scenario created: lowest price exceeds your max.", "warning")
                return redirect(url_for("workflow_history"))
            amount = min(max_price, 129)
            steps = [
                f"Agent: Search '{item_name}' with rating ≥ {min_rating} and delivery ≤ {max_delivery_days} days.",
                f"Agent: Select candidate item at ${amount}.",
                "Agent: Prepare order and await payment authorization.",
                f"AMOUNT={amount}",
            ]
            wid = _insert_workflow("ProductPurchase", steps, "awaiting_payment")
            flash("Scenario created. Review and authorize payment.", "info")
            return redirect(url_for("scenario_pay", workflow_id=wid))
        return render_template("scenario_product.html", csrf_token=get_csrf_token())

    @app.route("/scenario/hotel", methods=["GET", "POST"])
    @login_required
    def scenario_hotel():
        if request.method == "POST":
            verify_csrf()
            location = request.form.get("location", "").strip()
            price_per_night = int(request.form.get("price_per_night", "0") or 0)
            checkin = request.form.get("checkin", "")
            checkout = request.form.get("checkout", "")
            min_rating = float(request.form.get("min_rating", "0") or 0)
            from datetime import date as _date, timedelta as _timedelta
            errors = []
            if not (location and price_per_night > 0 and checkin and checkout):
                errors.append("Please fill all required fields.")
            try:
                d1 = _date.fromisoformat(checkin)
                d2 = _date.fromisoformat(checkout)
            except Exception:
                errors.append("Invalid date format."); d1 = d2 = None
            today = _date.today()
            horizon = today + _timedelta(days=365)
            if d1 and d1 < today: errors.append("Check-in date cannot be in the past.")
            if d2 and d2 < today: errors.append("Check-out date cannot be in the past.")
            if d1 and d2 and d2 <= d1: errors.append("Check-out date must be after check-in date.")
            if d1 and d1 > horizon: errors.append("Check-in date must be within one year from today.")
            if d2 and d2 > horizon: errors.append("Check-out date must be within one year from today.")
            if errors:
                for e in errors: flash(e, "danger")
                return render_template("scenario_hotel.html", csrf_token=get_csrf_token())
            nights = max(1, (d2 - d1).days)
            if random.random() < 0.30:
                found_nightly = max(price_per_night + 20, int(price_per_night * 1.15))
                steps = [
                    f"Agent: Search hotels in {location} rating ≥ {min_rating}, ≤ ${price_per_night}/night for {nights} night(s).",
                    f"Agent: Found offers starting at ${found_nightly}/night, which exceeds your max ${price_per_night}/night.",
                    "Agent: Declined creating a payable scenario."
                ]
                _insert_workflow("HotelBooking", steps, "rejected")
                flash("No scenario created: nightly rate exceeds your max.", "warning")
                return redirect(url_for("workflow_history"))
            amount = min(200 * nights, price_per_night * nights)
            steps = [
                f"Agent: Search hotels in {location} rating ≥ {min_rating}, ≤ ${price_per_night}/night for {nights} night(s).",
                f"Agent: Select candidate at ${price_per_night}/night.",
                "Agent: Prepare booking and await payment authorization.",
                f"AMOUNT={amount}",
            ]
            wid = _insert_workflow("HotelBooking", steps, "awaiting_payment")
            flash("Scenario created. Review and authorize payment.", "info")
            return redirect(url_for("scenario_pay", workflow_id=wid))
        return render_template("scenario_hotel.html", csrf_token=get_csrf_token())

    @app.route("/scenario/flight", methods=["GET", "POST"])
    @login_required
    def scenario_flight():
        if request.method == "POST":
            verify_csrf()
            airline = request.form.get("airline", "").strip()
            depart_date = request.form.get("depart_date", "")
            return_date = request.form.get("return_date", "")
            price_ceiling = int(request.form.get("price_ceiling", "0") or 0)
            from datetime import date as _date, timedelta as _timedelta
            errors = []
            if not (depart_date and price_ceiling > 0):
                errors.append("Please provide at least a depart date and price ceiling.")
            try:
                d_dep = _date.fromisoformat(depart_date)
            except Exception:
                d_dep = None; errors.append("Invalid depart date.")
            d_ret = None
            if return_date:
                try:
                    d_ret = _date.fromisoformat(return_date)
                except Exception:
                    errors.append("Invalid return date.")
            today = _date.today()
            horizon = today + _timedelta(days=365)
            if d_dep and d_dep < today: errors.append("Depart date cannot be in the past.")
            if d_ret and d_ret < today: errors.append("Return date cannot be in the past.")
            if d_dep and d_ret and d_ret <= d_dep: errors.append("Return date must be after depart date.")
            if d_dep and d_dep > horizon: errors.append("Depart date must be within one year from today.")
            if d_ret and d_ret > horizon: errors.append("Return date must be within one year from today.")
            if errors:
                for e in errors: flash(e, "danger")
                return render_template("scenario_flight.html", csrf_token=get_csrf_token())
            if random.random() < 0.30:
                found = max(price_ceiling + 30, int(price_ceiling * 1.15))
                rt = f" return {return_date}" if return_date else ""
                al = f" on {airline}" if airline else ""
                steps = [
                    f"Agent: Search flights{al} depart {depart_date}{rt} under ${price_ceiling}.",
                    f"Agent: Found itineraries starting at ${found}, which exceeds your max ${price_ceiling}.",
                    "Agent: Declined creating a payable scenario."
                ]
                _insert_workflow("FlightPurchase", steps, "rejected")
                flash("No scenario created: fare exceeds your max.", "warning")
                return redirect(url_for("workflow_history"))
            amount = min(price_ceiling, 199)
            rt = f" return {return_date}" if return_date else ""
            al = f" on {airline}" if airline else ""
            steps = [
                f"Agent: Search flights{al} depart {depart_date}{rt} under ${price_ceiling}.",
                f"Agent: Select candidate itinerary at ${amount}.",
                "Agent: Prepare ticketing and await payment authorization.",
                f"AMOUNT={amount}",
            ]
            wid = _insert_workflow("FlightPurchase", steps, "awaiting_payment")
            flash("Scenario created. Review and authorize payment.", "info")
            return redirect(url_for("scenario_pay", workflow_id=wid))
        return render_template("scenario_flight.html", csrf_token=get_csrf_token())

    @app.route("/scenario/pay/<int:workflow_id>", methods=["GET", "POST"])
    @login_required
    def scenario_pay(workflow_id: int):
        db = get_db()
        row = db.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?",
                         (workflow_id, session['user_id'])).fetchone()
        if not row: abort(404)
        steps = json.loads(row["steps"]) if row["steps"] else []
        amount = _extract_amount_from_steps(steps) or 0
        if request.method == "POST":
            verify_csrf()
            token = _ensure_vault_token()
            if not token:
                flash("No vaulted payment method found. Add one in Vault first.", "warning")
                return redirect(url_for("vault"))
            ok, message = mock_charge(session["user_id"], token, amount)
            steps.append(f"Payment Gateway: {message}")
            status = "success" if ok else "failed"
            db.execute("UPDATE workflows SET steps = ?, status = ? WHERE id = ?",
                       (json.dumps(steps), status, workflow_id))
            db.commit()
            if ok: flash("Payment approved (mock).", "success")
            else:  flash(message, "danger")
            return redirect(url_for("workflow_history"))
        return render_template("scenario_pay.html", item={
            "id": row["id"], "name": row["name"], "steps": steps, "status": row["status"],
            "created_at": row["created_at"], "amount": amount,
        }, csrf_token=get_csrf_token())

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
                "steps": json.loads(r["steps"]) if r["steps"] else [],
                "created_at": r["created_at"],
            })
        return render_template("workflows.html", items=items)

    with app.app_context():
        init_db()

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": get_csrf_token()}

    return app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
