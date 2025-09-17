#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agentic Banking Demo (smolagents + Helium/Selenium, Firefox)
Goal:
  1) Navigate to Amex-e site (your own test clone)
  2) Log in (HITL or auto if test fields are open)
  3) Read/display current balance
  4) Navigate to the Transfer page (no real transfer submitted)

Requirements:
  pip install helium selenium webdriver-manager pillow python-dotenv
  pip install smolagents transformers

"""

from __future__ import annotations

import os
import re
import time
from time import sleep
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple, Any

from dotenv import load_dotenv
import helium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from smolagents import CodeAgent, tool, TransformersModel

load_dotenv()

firefox_options = webdriver.FirefoxOptions()
firefox_options.add_argument("--force-device-scale-factor=1")
firefox_options.add_argument("--window-size=1280,1200")
firefox_options.add_argument("--window-position=0,0")
firefox_options.set_preference("intl.accept_languages", "en-US, en")
firefox_options.set_preference("dom.webnotifications.enabled", False)
firefox_options.set_preference("dom.push.enabled", False)
firefox_options.set_preference("privacy.trackingprotection.enabled", True)

driver = helium.start_firefox(headless=False, options=firefox_options)


def _drv():
    d = helium.get_driver()
    if d is None:
        raise RuntimeError("Helium driver not initialized")
    return d

def _wait_css(sel: str, timeout: int = 30):
    return WebDriverWait(_drv(), timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
    )

def _try_click(by: By, val: str, timeout: int = 8) -> bool:
    try:
        el = WebDriverWait(_drv(), timeout).until(
            EC.element_to_be_clickable((by, val))
        )
        _drv().execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        _drv().execute_script("arguments[0].click();", el)
        return True
    except Exception:
        return False

def _exists(by: By, val: str) -> bool:
    try:
        _drv().find_element(by, val)
        return True
    except Exception:
        return False

def _find_first(candidates: List[Tuple[str, str]]):
    """Return first WebElement matching the (how, selector) candidates list."""
    d = _drv()
    for how, sel in candidates:
        by = By.CSS_SELECTOR if how == "css" else By.XPATH
        els = d.find_elements(by, sel)
        if els:
            return els[0]
    return None

def _click_candidates(candidates: List[Tuple[str, str]]) -> bool:
    el = _find_first(candidates)
    if el is None:
        return False
    _drv().execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    _drv().execute_script("arguments[0].click();", el)
    return True

def _norm(s: Optional[str]) -> str:
    return (s or "").strip()

def _to_float_currency(text: str) -> Optional[float]:
    """
    Parse currency-like text: "$1,234.56" -> 1234.56
    Works for most EN formats; adjust as needed for your clone.
    """
    if not text:
        return None
    t = text.replace("\u00A0", "").replace("\u202F", "")
    t = re.sub(r"[^\d.,-]", "", t)

    if t.count(",") > 1 and "." in t:
        t = t.replace(",", "")
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None



SELECTORS: Dict[str, List[Tuple[str, str]]] = {
    "HEADER_LOGIN": [
        ("css", "a[id='gnav_login'], a[href*='login'], button[data-testid='login']"),
        ("xpath", "//a[contains(@href,'login')][contains(.,'Sign in') or contains(.,'Log in') or contains(.,'Sign In')]"),
        ("xpath", "//button[contains(.,'Sign in') or contains(.,'Log in')]"),
    ],
    "LOGIN_USERNAME": [
        ("css", "input[id='eliloUserID']"),
        ("xpath", "//input[@id='userid' or @name='username' or @type='email']"),
    ],
    "LOGIN_PASSWORD": [
        ("css", "input[id='eliloPassword']"),
        ("xpath", "//input[@id='password' or @name='password' or @type='password']"),
    ],
    "LOGIN_SUBMIT": [
        ("css", "button#login-submit, button[type='submit'], button[data-testid='login-submit']"),
        ("xpath", "//button[@type='submit' or @id='login-submit' or @data-testid='login-submit']"),
    ],
    "DASHBOARD_MARKER": [
        ("css", "[data-testid='dashboard'], .dashboard, main[aria-label*='Dashboard']"),
        ("xpath", "//*[contains(@class,'dashboard') or @data-testid='dashboard' or contains(@aria-label,'Dashboard')]"),
    ],
    "BALANCE_VALUE": [
        ("css", "#account-balance .amount, [data-testid='account-balance-amount'], .balance-amount"),
        ("xpath", "//*[@id='account-balance']//*[contains(@class,'amount') or @data-testid='account-balance-amount']"),
        ("xpath", "//*[contains(@class,'balance')]//*[contains(@class,'amount') or contains(@class,'value')][1]"),
    ],
    "NAV_TRANSFER": [
        ("css", "a[href*='transfer'], a[data-testid='nav-transfer'], button[data-testid='nav-transfer']"),
        ("xpath", "//a[contains(@href,'transfer') or contains(.,'Transfer')] | //button[contains(.,'Transfer')]"),
    ],
    "TRANSFER_MARKER": [
        ("css", "[data-testid='transfer-page'], .transfer-form, form[action*='transfer']"),
        ("xpath", "//*[@data-testid='transfer-page' or contains(@class,'transfer-form') or self::form[contains(@action,'transfer')]]"),
    ],
    "COOKIE_ACCEPT": [
        ("css", "button[aria-label*='Accept'][aria-label*='cookies'], #onetrust-accept-btn-handler"),
        ("xpath", "//button[contains(.,'Accept') and contains(.,'cookie')]"),
    ],
}


@tool
def go_to(url: str) -> str:
    """Open a URL in the current browser tab.

    Args:
        url (str): The absolute URL to open.

    Returns:
        str: A confirmation message with the navigated URL.
    """
    _drv().get(url)
    return f"Navigated to: {url}"

@tool
def click_text(text: str) -> str:
    """Click a clickable element by its visible text (button/link/etc.).

    Args:
        text (str): Visible label to click.

    Returns:
        str: Click confirmation.
    """
    helium.click(text)
    return f"Clicked text: {text}"

@tool
def write_text(text: str) -> str:
    """Type text into the currently focused input element.

    Args:
        text (str): The text to type.

    Returns:
        str: Preview of typed text.
    """
    helium.write(text)
    return f"Typed: {text[:60]}{'...' if len(text) > 60 else ''}"

@tool
def press(key: str) -> str:
    """Press a keyboard key (selenium Keys names, e.g., 'ENTER', 'ESCAPE').

    Args:
        key (str): Key name (case-insensitive).

    Returns:
        str: Press confirmation.
    """
    key_obj = getattr(Keys, key.upper(), None)
    if not key_obj:
        raise ValueError(f"Unsupported key: {key}")
    webdriver.ActionChains(_drv()).send_keys(key_obj).perform()
    return f"Pressed: {key}"

@tool
def close_popups() -> str:
    """Try to dismiss popups (ESC several times) and accept cookie banner if present.

    Returns:
        str: Status string of the attempt.
    """
    d = _drv()
    chain = webdriver.ActionChains(d)
    for _ in range(3):
        chain.send_keys(Keys.ESCAPE).perform()
        sleep(0.2)
    _click_candidates(SELECTORS["COOKIE_ACCEPT"])
    return "Popups/consent dismissed if present."


@tool
def current_url() -> str:
    """
    Return the current page URL.

    Returns:
        str: 'current url
    """
    return f"URL: {_drv().current_url}"

@tool
def human_gate(message: str = "Complete any required human step (e.g., CAPTCHA/2FA), then press ENTER in console.") -> str:
    """Pause execution to allow human to complete blocked steps.

    Args:
        message (str): Message printed to console.

    Returns:
        str: 'human_done' when resumed.
    """
    print("\n================ HUMAN GATE ================\n" + message + "\n===========================================\n")
    try:
        input()
    except EOFError:
        for _ in range(6):
            print("waiting...")
            sleep(10)
    return "human_done"

@tool
def finish_session() -> str:
    """
    Quit the browser session gracefully.
    
    Returns:
        str: Status.
    """
    try:
        helium.kill_browser()
        return "Browser closed."
    except Exception:
        return "Browser already closed."


@tool
def amex_go_home(base_url: str) -> str:
    """Open the Amex site home/landing page.

    Args:
        base_url (str): Base URL of site 

    Returns:
        str: Navigation status.
    """
    _drv().get(base_url)
    return f"Opened: {base_url}"

@tool
def amex_header_sign_in() -> str:
    """Click the header 'Sign in / Log in' entry if present.

    Returns:
        str: Whether header login was clicked.
    """
    clicked = _click_candidates(SELECTORS["HEADER_LOGIN"])
    return "Header login clicked." if clicked else "Header login not found."

@tool
def amex_fill_username(username: str) -> str:
    """Fill the username/email field on the login page.

    Args:
        username (str): Username or email for test account.

    Returns:
        str: Status string.
    """
    el = _find_first(SELECTORS["LOGIN_USERNAME"])
    if not el:
        return "Username field not found."
    el.clear()
    el.send_keys(username)
    return "Username filled."

@tool
def amex_fill_password(password: str) -> str:
    """Fill the password field on the login page.

    Args:
        password (str): Password for test account.

    Returns:
        str: Status string.
    """
    el = _find_first(SELECTORS["LOGIN_PASSWORD"])
    if not el:
        return "Password field not found."
    el.clear()
    el.send_keys(password)
    return "Password filled."

@tool
def amex_submit_login() -> str:
    """
    Submit the login form (click submit or press ENTER).

    Returns:
        str: 'message'
    """
    if _click_candidates(SELECTORS["LOGIN_SUBMIT"]):
        return "Login submit clicked."
    pwd = _find_first(SELECTORS["LOGIN_PASSWORD"])
    if pwd:
        pwd.send_keys(Keys.ENTER)
        return "Login submitted via ENTER."
    return "Login submit control not found."

@tool
def amex_is_login_context() -> str:
    """Heuristically detect if login UI is visible.

    Returns:
        str: 'login_context=True|False'
    """
    has_user = _find_first(SELECTORS["LOGIN_USERNAME"]) is not None
    has_pwd = _find_first(SELECTORS["LOGIN_PASSWORD"]) is not None
    url = (_drv().current_url or "").lower()
    looks_like_login = ("login" in url) or has_user or has_pwd
    return f"login_context={looks_like_login}"

@tool
def amex_is_dashboard() -> str:
    """Check if dashboard context (post-login) is present.

    Returns:
        str: 'dashboard=True|False'
    """
    el = _find_first(SELECTORS["DASHBOARD_MARKER"])
    return f"dashboard={el is not None}"

@tool
def amex_get_balance() -> str:
    """Extract and return the displayed account balance as text and numeric value.

    Returns:
        str: e.g., 'balance_text=$1,234.56; balance_value=1234.56' or error message.
    """
    el = _find_first(SELECTORS["BALANCE_VALUE"])
    if not el:
        return "balance_not_found"
    txt = _norm(el.text)
    val = _to_float_currency(txt)
    return f"balance_text={txt}; balance_value={val if val is not None else 'unknown'}"

@tool
def amex_nav_to_transfer() -> str:
    """Click navigation entry to reach the Transfer page.

    Returns:
        str: 'transfer_nav_clicked' or error.
    """
    if _click_candidates(SELECTORS["NAV_TRANSFER"]):
        try:
            WebDriverWait(_drv(), 20).until(
                lambda d: _find_first(SELECTORS["TRANSFER_MARKER"]) is not None
            )
        except Exception:
            pass
        return "transfer_nav_clicked"
    return "transfer_nav_not_found"

@tool
def amex_is_transfer_page() -> str:
    """Check if the transfer page (or form) is visible.

    Returns:
        str: 'transfer_page=True|False'
    """
    el = _find_first(SELECTORS["TRANSFER_MARKER"])
    return f"transfer_page={el is not None}"



AGENT_SYSTEM_PROMPT = """
You are an autonomous banking assistant operating a REAL browser. 
Follow the steps and tool semantics strictly. Never perform any real transfer or irreversible action.

Steps:
- Open the base URL (amex_go_home) and login using amex_header_sign_in()
- Check amex_is_login_context(). 
  - If it returns login_context=True:
    - If credentials are provided in task context, call amex_fill_username(username) and amex_fill_password(password), then amex_submit_login().
    - Otherwise call human_gate("Please complete login (and any CAPTCHA/2FA) then press ENTER.").
- Wait until amex_is_dashboard() returns dashboard=True. If blocked or stuck, call human_gate() and retry.
- Read balance via amex_get_balance() and record the returned balance_text/balance_value.
- Navigate to the transfer page via amex_nav_to_transfer().
- Verify arrival with amex_is_transfer_page(); SUCCESS only if it returns transfer_page=True.
- Once transfer_page=True, stop the run: call finish_session() and do not proceed further.

Tool semantics (you MUST honor exact return conventions):
- amex_go_home(base_url): Open the site's landing page
- amex_header_sign_in(): Click header sign-in. Returns "Header login clicked." or "Header login not found."
- amex_is_login_context(): Login UI check. Returns "login_context=True" or "login_context=False".
- amex_fill_username(username): Fill username/email field. Returns "Username filled." or "Username field not found."
- amex_fill_password(password): Fill password field. Returns "Password filled." or "Password field not found."
- amex_submit_login(): Submit login (click submit / ENTER fallback). Returns "Login submit clicked." / "Login submitted via ENTER." / "Login submit control not found."
- amex_is_dashboard(): Dashboard presence check. Returns "dashboard=True" or "dashboard=False".
- amex_get_balance(): Extract balance. Returns "balance_text=$X,XXX.XX; balance_value=NNNN.NN" or "balance_not_found".
- amex_nav_to_transfer(): Click nav to Transfer page. Returns "transfer_nav_clicked" or "transfer_nav_not_found".
- amex_is_transfer_page(): Transfer page check. Returns "transfer_page=True" or "transfer_page=False". 
  - You must consider the goal reached ONLY if "transfer_page=True".
- close_popups(): Dismiss modals/cookie banners (e.g., ESC and consent button). Returns a status string.
- scroll_down(pixels=1000): Scroll the page. Returns "Scrolled down <pixels>px".
- current_url(): Returns "URL: <current_url>".
- human_gate(message="..."): Pause for human to complete CAPTCHA/2FA or manual steps; continue after ENTER. Returns "human_done".
- finish_session(): Close the browser session.

Operational rules:
- Use ONLY the registered tools; do not define new functions. Be concise in tool usage.
- After each action, rely on the updated page state. If an expected element is missing, try close_popups(), scroll_down(), and re-check.
- If login or navigation is blocked by CAPTCHA/2FA, call human_gate() with a clear instruction and then continue.
- Do NOT type or submit any sensitive information unless explicitly provided in the task context.
- Stop immediately after amex_is_transfer_page() returns transfer_page=True and call finish_session().

"""

@dataclass
class RunSpec:
    base_url: str
    username: Optional[str] = None
    password: Optional[str] = None


def build_agent(max_steps: int = 30) -> CodeAgent:
    model = TransformersModel(model_id="meta-llama/Llama-3.1-8B-Instruct") # "meta-llama/Llama-3.1-8B-Instruct"
    agent = CodeAgent(
        tools=[
            go_to, click_text, write_text, press, close_popups, current_url,
            human_gate, finish_session,
            amex_go_home, amex_header_sign_in, amex_fill_username, amex_fill_password,
            amex_submit_login, amex_is_login_context, amex_is_dashboard,
            amex_get_balance, amex_nav_to_transfer, amex_is_transfer_page,
        ],
        model=model,
        max_steps=max_steps,
        verbosity_level=2,
        additional_authorized_imports=["helium"],
    )
    agent.python_executor("from helium import *")
    return agent


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Agentic login -> balance -> transfer nav")
    ap.add_argument("--base-url", default="https://www.americanexpress.com/?inav=NavLogo")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--username", default=os.getenv("AMEX_USER"))
    ap.add_argument("--password", default=os.getenv("AMEX_PASS"))
    args = ap.parse_args()

    spec = RunSpec(base_url=args.base_url, username=args.username, password=args.password)
    agent = build_agent(max_steps=args.max_steps)

    task = f"""
Base URL: {spec.base_url}
Test credentials (optional): username={spec.username or 'HITL'}, password={'***' if spec.password else 'HITL'}

Print each steps' description for users.
Store username and password as variables. 

Steps to perform on the  site:
- amex_go_home(base_url)
- close_popups()
- amex_header_sign_in()
- If amex_is_login_context says True:
    - If username/password provided, call amex_fill_username/amex_fill_password, then amex_submit_login
    - Else call human_gate() and let a human complete login
- Wait and check amex_is_dashboard; if blocked by 2FA/CAPTCHA, call human_gate()
- Call amex_get_balance and report it
- Call amex_nav_to_transfer and verify with amex_is_transfer_page
Stop once the transfer page is visible. Do NOT submit any transfer.
"""

    out = agent.run(task + "\n\n" + AGENT_SYSTEM_PROMPT)
    print("\n=== FINAL OUTPUT ===")
    print(out)


if __name__ == "__main__":
    try:
        main()
    finally:
        pass
