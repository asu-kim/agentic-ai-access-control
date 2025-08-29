# A Prototype Framework for Agentic AI's Access Control

## ⚠️ Security Notes 

- **Do NOT use real payment details or addresses.** Use fake data only.
- Enable HTTPS and set `SESSION_COOKIE_SECURE=True` in production.
- Keep `SECRET_KEY` and `VAULT_KEY` out of source control.


## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate  
# Windows: .venv\Scripts\activate

pip install -r requirements.txt

# (Optional) set consistent keys in production
export SECRET_KEY="change-me"
export VAULT_KEY="$(python - <<'PY'
from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())
PY
)"
# export PREVIOUS_VAULT_KEYS="old-key-1,old-key-2"

python app.py
# open http://127.0.0.1:5000 in the web browser
```

If your shell cannot find `pip`, use:
```bash
python -m pip install -r requirements.txt
# or
pip3 install -r requirements.txt
```

## Usage
1. Register → Sign in
2. **Vault**: add a mock card/address (stored encrypted). You’ll get a token.
3. **Dashboard → Agent Scenario Builder**: pick *Product/Hotel/Flight* and fill options to create scenario.
4. A scenario is created in **Workflows** with `status=awaiting_payment` and a step like `AMOUNT=...`.
5. Open the scenario’s **Authorize payment** page and click **Authorize with vaulted token**.
   - Mock gateway approves ≤ **$200**; rejects otherwise.



Minimal demo app showing:
- Account signup/login with session idle-timeout (10 min)
- Encrypted Vault for payment data (Fernet) with tokenization
- Scenario builders: **Product purchase**, **Hotel booking**, **Flight purchase**
- Two-step payment: create scenario → review & authorize via **vault token**
- CSRF + basic security headers

- Hotel: no past dates; checkout after check-in; both within **1 year** from today.
- Flight: no past dates; return after depart; both within **1 year** from today.
- Scenario creation may be **randomly rejected (30%)** if the agent find the products with market price > user max (product/hotel/flight).


## Notes
- Vault encryption uses **Fernet** with `VAULT_KEY`. If you rotate keys, provide old keys via `PREVIOUS_VAULT_KEYS` and run `flask --app app migrate-keys`.
- This is a *demo*. Do not store real payment data.
- The included `app.db` is initialized with empty tables. The app can also create it automatically on first run.


## File Structure
```
.
├─ app.py
├─ app.db                # included for convenience (created/initialized)
├─ requirements.txt
├─ README.md
├─ static/
│  ├─ app.js
│  └─ style.css
└─ templates/
   ├─ base.html
   ├─ index.html
   ├─ login.html
   ├─ register.html
   ├─ dashboard.html
   ├─ vault.html
   ├─ vault_view.html
   ├─ workflows.html
   ├─ scenario_product.html
   ├─ scenario_hotel.html
   └─ scenario_flight.html
   └─ scenario_pay.html
```
