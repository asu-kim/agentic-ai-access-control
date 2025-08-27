# Agentic AI Demo – Flask Login + Vault + Agent Simulator

A research-oriented Flask prototype showcasing secure handling of sensitive data for Agentic AI workflows. It includes authentication, a tokenized **Data Vault** (encrypted), an **Agent Simulator** (“Book a hotel under $200”), **workflow logging**, a **10-minute idle auto-logout**, a **“Task completed → auto sign-out”** control, and a **remaining session time** indicator.

---

## Feature Map

### Authentication & Session
- **Sign up / Sign in / Sign out** with salted password hashing (`werkzeug.security`).
- **Login-protected dashboard** (`/dashboard`).
- **Task Completed → auto sign-out**: a dashboard checkbox posts to `/complete`, verifies CSRF, and immediately logs out.
- **Idle timeout (10 minutes)**: if there is no request activity for 10 minutes, the session expires on the next request.
- **Remaining session time** is displayed on the dashboard and refreshed every 5 seconds via `/session-remaining`.

### Data Vault (Encrypted, Tokenized)
- **Mock Credit Card / Address** form at `/vault`.
- Values are **encrypted at rest** with **Fernet** (`cryptography`) and stored in table `vault` as an opaque BLOB.
- After saving, the user gets a **token**. UI **never** shows raw values; `/vault/<token>` provides only **masked** fields.
- Server-only helpers decrypt with **`VAULT_KEY`**. Agents/clients never see raw data—only the token.

### Agent Simulator (Mock Booking)
- Dashboard button: **“Book a hotel under $200”** → POST `/agent/book-hotel`.
- Simulates an agent workflow:
  1) Search hotels under $200  
  2) Choose a candidate (e.g., $179/night)  
  3) Request payment with **vault token**  
  4) Server-side **`mock_charge(user_id, token, amount)`** (no external PSP)
- All steps are appended to `workflows` (JSON) and shown on `/workflows`.

### CSRF & Security Headers
- Lightweight **CSRF token** in forms (session token vs hidden form field).
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, and a restrictive `Content-Security-Policy` (self).

---

## Architecture & Data Model

**SQLite** is used and auto-initialized on first run (`app.db`).

Tables:
- **`users`**
  - `id`, `username` (unique), `email` (unique), `password_hash`, `created_at`
- **`vault`**
  - `id`, `user_id` (FK), `token` (unique), `blob` (Fernet-encrypted JSON), `created_at`
- **`workflows`**
  - `id`, `user_id` (FK), `name`, `steps` (JSON array), `status`, `created_at`

**Session keys:** `user_id`, `username`, `csrf_token`, `last_seen` (ISO UTC), and `permanent=True`.

---

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate               # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py                           # open http://127.0.0.1:5000
```

If your shell cannot find `pip`, use:
```bash
python -m pip install -r requirements.txt
# or
pip3 install -r requirements.txt
```

**Initialize DB manually (optional; auto-runs on first boot too):**
```bash
flask --app app.py init-db
```

---

## Environment Variables

- **`SECRET_KEY`** – Flask session signing key (required in production).
- **`VAULT_KEY`** – Base64 Fernet key for vault encryption.  
  - If not provided, a new key is generated at startup (demo only). That will **invalidate existing tokens** on each restart.
  - In production, set and persist a stable key. Plan for **key rotation**.

Optional:
- `FLASK_RUN_PORT`, etc., if you prefer `flask run`.

---

## Routes

- `GET /` – Home  
- `GET|POST /register` – Sign up  
- `GET|POST /login` – Sign in  
- `GET /logout` – Sign out  
- `GET /dashboard` – Protected area  
- `POST /complete` – Dashboard **Task completed → auto sign-out**  
- `GET|POST /vault` – Create/update vaulted card/address (encrypted)  
- `GET /vault/<token>` – Masked view of tokenized data (no raw values)  
- `POST /agent/book-hotel` – Agent simulator: mock booking under $200 using a vault token  
- `GET /workflows` – View workflow logs (steps)  
- `GET /session-remaining` – JSON `{ "seconds": N }` for the countdown UI  

---

## Typical Flow

1. **Register → Sign in**
2. Go to **Vault** and add **mock card/address** → receive **token**
3. Go to **Dashboard** and click **“Book a hotel under $200”**
4. Inspect **Workflows** to see the recorded steps
5. Try the **Task completed** checkbox (auto sign-out)
6. Leave the session idle for 10 minutes and observe auto-expiry on the next request

---

## Security Notes (Demo)

- **Do NOT use real payment details.** Use fake data only.
- Enable HTTPS and set `SESSION_COOKIE_SECURE=True` in production.
- Keep `SECRET_KEY` and `VAULT_KEY` out of source control.
- CSRF is basic; consider **Flask-WTF** for stricter protection.
- The mock PSP (`mock_charge`) is illustrative. Before integrating a real PSP, add: explicit user approvals, audit logs, rate limiting, and policy enforcement.

---

