#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agentic Amazon Shopper (smolagents + Helium/Selenium, Firefox)
- Agent decides which tools to call (no fixed step order).
- Enforces a MAX price (server-side filter + product page validation).
- Handles pagination, variant selection, add-to-cart, and go-to-checkout.
- Pauses for human at login/CAPTCHA/address/payment.

Run examples:
  python amazon.py --query "wireless mouse" --max 25
  python amazon.py --query "ssd 2tb" --max 120 --pages 7

Notes:
- Uses Firefox via Helium (geckodriver required on PATH).
- Best run with GUI (headless=False) so you can solve prompts.
"""

import os
import re
import argparse
from io import BytesIO
from time import sleep
from typing import Optional, Tuple, List
from dataclasses import dataclass
from PIL import Image

from dotenv import load_dotenv
import helium
from helium import Link, Text, click, scroll_down
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from smolagents import CodeAgent, tool, TransformersModel
from smolagents.agents import ActionStep
from smolagents import Model

from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

load_dotenv()

temp_dir = f"~/data/tmp/helium_data_{os.getpid()}"
firefox_options = webdriver.FirefoxOptions()
firefox_options.add_argument("--force-device-scale-factor=1")
firefox_options.add_argument("--window-size=1200,1400")
firefox_options.add_argument("--window-position=0,0")
firefox_options.set_preference("intl.accept_languages", "en-US, en")

driver = helium.start_firefox(headless=False, options=firefox_options)

from urllib.parse import urlparse, parse_qs

def _is_checkout_spc_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        query = parse_qs(parsed.query or "")

        if "/checkout/p/" not in path or "/spc" not in path:
            return False

        pipeline = (query.get("pipelineType", [""])[0] or "").lower()
        if pipeline != "chewbacca":
            return False

        return True
    except Exception:
        return False


def _wait_css(sel: str, timeout: int = 30):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
    )

def _try_click(by: By, val: str,timeout: int, retries: int = 2, center: bool = True) -> bool:
    """
    Try to click an element found by (by, val) quickly without waiting for clickable.
    Returns True if any click attempt succeeds, else False.
    """
    for attempt in range(retries):
        try:
            elems = driver.find_elements(by, val)
            if not elems:
                continue
            el = next((e for e in elems if e.is_displayed()), elems[0])
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});", el
            )
            driver.execute_script("window.scrollBy(0, -80);")
            try:
                el.click()
                return True
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", el)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


def _exists(by: By, val: str) -> bool:
    try:
        driver.find_element(by, val)
        return True
    except Exception:
        return False

def _norm_txt(t: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", t or "").strip()

def _to_float(num: str) -> Optional[float]:
    s = _norm_txt(num).replace("\u00A0","").replace("\u202F","")
    s = re.sub(r"[^0-9.,]", "", s)
    if s.count(".") > 1 and "," in s:
        s = s.replace(".", "")
    s = s.replace(",", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None

def _current_is_signin_or_captcha() -> str:
    url = (driver.current_url or "").lower()
    if "validatecaptcha" in url or "captcha" in url:
        return "captcha"
    if "ap/signin" in url or _exists(By.ID, "ap_email") or _exists(By.ID, "ap_password"):
        return "signin"
    return ""

def _results_ready(timeout: int = 40):
    _wait_css("div.s-main-slot", timeout=timeout)
    _wait_css("[data-component-type='s-search-result']", timeout=timeout)

def _close_common_banners():
    for by, val in [
        (By.ID, "sp-cc-accept"),
        (By.CSS_SELECTOR, "input#sp-cc-accept"),
        (By.CSS_SELECTOR, "input[name='accept']"),
    ]:
        _try_click(by, val, timeout=2)
    for by, val in [
        (By.CSS_SELECTOR, "button[data-action='a-popover-close']"),
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
    ]:
        _try_click(by, val, timeout=2)

def _card_price(card) -> Optional[float]:
    try:
        txt = card.find_element(By.CSS_SELECTOR, "span.a-offscreen").text
        v = _to_float(txt)
        if v is not None: return v
    except Exception:
        pass
    try:
        whole = card.find_element(By.CSS_SELECTOR, "span.a-price .a-price-whole").text
        frac  = card.find_element(By.CSS_SELECTOR, "span.a-price .a-price-fraction").text
        v = _to_float(f"{whole}.{frac}")
        if v is not None: return v
    except Exception:
        pass
    # fallback
    try:
        raw = card.find_element(By.CSS_SELECTOR, "span.a-price").text
        m = re.search(r"(\d[\d,.\s\u00A0\u202F]*)", raw)
        if m: return _to_float(m.group(1))
    except Exception:
        pass
    return None


def _dp_link(card) -> Tuple[Optional[str], Optional[str]]:
    try:
        a = card.find_element(By.CSS_SELECTOR, "h2 a.a-link-normal")
        href = a.get_attribute("href") or ""
        if "/dp/" in href or "/gp/" in href:
            return _norm_txt(a.text), href
    except Exception:
        pass
    return None, None

def _go_next_page() -> bool:
    try:
        nxt = driver.find_element(By.CSS_SELECTOR, "a.s-pagination-next")
        if "disabled" in (nxt.get_attribute("class") or ""):
            return False
        driver.execute_script("arguments[0].click();", nxt)
        _results_ready(timeout=30)
        return True
    except Exception:
        return False

def _product_price() -> Optional[float]:
    for sel in [
        "#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "#apex_desktop span.a-offscreen",
        "span.a-offscreen",
        "#priceblock_ourprice", "#priceblock_dealprice", "#priceblock_saleprice",
        ".reinventPricePriceToPayString",
    ]:
        try:
            v = _to_float(driver.find_element(By.CSS_SELECTOR, sel).text)
            if v is not None:
                return v
        except Exception:
            continue
    return None

def _select_any_variant():
    for sel in ["#native_dropdown_selected_size_name", "select[name='dropdown_selected_size_name']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            opts = el.find_elements(By.TAG_NAME, "option")
            for opt in opts:
                txt = _norm_txt(opt.text).lower()
                if txt and "select" not in txt:
                    driver.execute_script("arguments[0].selected = true;", opt)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", el)
                    sleep(0.6)
                    return True
        except Exception:
            pass
    for sel in ["div#variation_color_name li", "div#variation_size_name li",
                "ul.a-unordered-list.a-nostyle.a-button-list.a-horizontal li"]:
        try:
            items = driver.find_elements(By.CSS_SELECTOR, sel)
            for sw in items:
                cls = (sw.get_attribute("class") or "").lower()
                if "selected" in cls or "unavailable" in cls or "disabled" in cls:
                    continue
                try:
                    btn = sw.find_element(By.CSS_SELECTOR, "button, a")
                except Exception:
                    btn = None
                try:
                    driver.execute_script("arguments[0].click();", btn or sw)
                    sleep(0.6)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
    return False

def _close_warranty_modal():
    for by, val in [
        (By.ID, "attachSiNoCoverage"),
        (By.CSS_SELECTOR, "input#attachSiNoCoverage"),
        (By.CSS_SELECTOR, "button[aria-labelledby='attachSiNoCoverage-announce']"),
    ]:
        if _try_click(by, val, timeout=2):
            break
    _try_click(By.ID, "attach-close_sideSheet-link", timeout=2)


@tool
def go_to(url: str) -> str:
    """Navigate the browser to a URL.

    Args:
        url (str): Destination address to open.

    Returns:
        str: Status message indicating the navigation target.
    """
    driver.get(url)
    return f"Navigated to {url}"

@tool
def go_back() -> str:
    """Go back one page in history.

    Returns:
        str: Status message indicating back navigation.
    """
    driver.back()
    return "Went back"

@tool
def finish_session() -> str:
    """Close the browser session.

    Returns:
        str: Status message indicating the browser was closed.
    """
    driver.quit()
    return "Browser closed"

@tool
def close_popups() -> str:
    """Dismiss visible modals or popups using the Escape key.

    Returns:
        str: Status message after sending Escape.
    """
    webdriver.ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    return "esc_sent"

@tool
def search_item_ctrl_f(text: str, nth_result: int = 1) -> str:
    """Find text on the current page and focus the nth occurrence.

    Args:
        text (str): Text snippet to search for in visible DOM nodes.
        nth_result (int): 1-based index of the occurrence to focus. Defaults to 1.

    Returns:
        str: Message with match count and which occurrence was focused.
    """
    elems = driver.find_elements(By.XPATH, f"//*[contains(text(), '{text}')]")
    if not elems:
        return f"no_match:{text}"
    idx = max(1, min(nth_result, len(elems))) - 1
    driver.execute_script("arguments[0].scrollIntoView(true);", elems[idx])
    return f"focused:{idx+1}/{len(elems)}:{text}"

@tool
def human_gate(message: str = "Complete required step, then press ENTER in console.") -> str:
    """Pause execution for human actions (e.g., login, CAPTCHA), then continue.

    Args:
        message (str): Prompt shown in the console before waiting.

    Returns:
        str: Status message after user resumes execution.
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
def amazon_stop_if_checkout_spc(close_browser: bool = True) -> str:
    """Stop the process when the current page is Amazon SPC checkout (Chewbacca).

    Args:
        close_browser (bool): If True, quit the Selenium driver to end the session. Defaults to True.

    Returns:
        str: "STOPPED_ON_CHECKOUT_SPC" if matched and (optionally) closed; 
             "NOT_AT_CHECKOUT_SPC" if the current page is not the SPC checkout.
    """
    current = driver.current_url or ""
    if _is_checkout_spc_url(current):
        try:
            _ = driver.find_elements(By.XPATH, "//*[contains(translate(.,'PLACE YOUR ORDER','place your order'),'place your order')]")
            _ = driver.find_elements(By.CSS_SELECTOR, "input[id='placeOrder'], #submitOrderButtonId")
            _ = driver.find_elements(By.CSS_SELECTOR, "input[name='placeYourOrder1']")
        except Exception:
            pass
        if close_browser:
            try:
                driver.quit()
            except Exception:
                pass
        return "STOPPED_ON_CHECKOUT_SPC"
    return "NOT_AT_CHECKOUT_SPC"

@tool
def amazon_open_results(query: str, max_price: float | None = None) -> str:
    """Open Amazon search results and optionally apply a server-side max price cap.

    Args:
        query (str): Product query to search on Amazon.
        max_price (float | None): Maximum price in USD to cap results (None to skip).

    Returns:
        str: Status message indicating results are open and the cap applied.
    """
    from urllib.parse import quote_plus
    q = quote_plus(query.strip())
    driver.get(f"https://www.amazon.com/s?k={q}")
    _close_common_banners()
    status = _current_is_signin_or_captcha()
    if status in ("captcha", "signin"):
        return f"blocked:{status}"
    _results_ready(timeout=40)
    if max_price is not None:
        try:
            url = driver.current_url
            cents = int(round(float(max_price) * 100))
            rh = f"p_36%3A-{cents}"
            url = re.sub(r"(&rh=[^&]*)", f"&rh={rh}", url) if "&rh=" in url else url + (("&" if "?" in url else "?") + f"rh={rh}")
            driver.get(url)
            _results_ready(timeout=10)
        except Exception:
            pass
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "span.a-dropdown-container")
            driver.execute_script("arguments[0].click();", btn)
            sleep(0.3)
            opt = driver.find_element(By.XPATH, "//a[contains(@href,'s?') and contains(., 'Price: Low to High')]")
            driver.execute_script("arguments[0].click();", opt)
            _results_ready(timeout=10)
        except Exception:
            pass
    return f"results_opened:{query}:cap={max_price}"


@tool
def amazon_next_results_page() -> str:
    """Advance to the next results page if available.

    Returns:
        str: "NEXT_OK" if moved, else "NO_NEXT".
    """
    moved = _go_next_page()
    return "NEXT_OK" if moved else "NO_NEXT"

@tool
def amazon_open_product() -> str:
    """Open a product detail page by URL.

    Returns:
        str: Status message confirming the product page opened.
    """
    link = driver.find_element(
        By.CSS_SELECTOR,
        "image[class='s-image']"
    )
    _try_click(link)
    _wait_css("body", timeout=10)
    return "product_opened"



@tool
def amazon_add_to_cart() -> str:
    """Click the Add to Cart button, retrying after variant selection if needed.

    Returns:
        str: "ADDED" if successful, else "ADD_FAILED_NEEDS_HUMAN".
    """
    for _ in range(2):    # (By.CSS_SELECTOR, "input#add-to-cart-button")
        for by, val in [(By.CSS_SELECTOR, "button[aria-lable='Add to cart']"), (By.CSS_SELECTOR, "input#Add to cart"),  (By.CSS_SELECTOR, "button[name='submit.addToCart']"), (By.XPATH, "//*[@id='add-to-cart-button']")]:
            if _try_click(by, val, timeout=6):
                sleep(2)
                _close_warranty_modal()
                return "ADDED"
    return "ADD_FAILED_NEEDS_HUMAN"

@tool
def amazon_proceed_to_checkout() -> str:
    """Proceed from cart overlay or cart page to the checkout flow.

    Returns:
        str: "CHECKOUT_FLOW" if in checkout, or "HUMAN_NEEDED_SIGNIN|HUMAN_NEEDED_CAPTCHA".
    """
    for by, val in [
        (By.ID, "attach-sidesheet-checkout-button"),
        (By.CSS_SELECTOR, "a#attach-sidesheet-checkout-button"),
        (By.NAME, "proceedToRetailCheckout"),
        (By.CSS_SELECTOR, "input[name='proceedToRetailCheckout']"),
        (By.CSS_SELECTOR, "a[name='sc-byc-ptc-button']"),
    ]:
        if _try_click(by, val, timeout=5):
            sleep(2)
            break
    if "checkout" not in (driver.current_url or ""):
        driver.get("https://www.amazon.com/gp/cart/view.html?ref_=nav_cart")
        _wait_css("body", timeout=30)
        if not _try_click(By.NAME, "proceedToRetailCheckout", timeout=12):
            _try_click(By.CSS_SELECTOR, "input[name='proceedToRetailCheckout']", timeout=8)
            -_try_click(By.CSS_SELECTOR, "a[name='sc-byc-ptc-button']")
    status = _current_is_signin_or_captcha()
    if status in ("captcha", "signin"):
        return f"HUMAN_NEEDED_{status.upper()}"
    return "CHECKOUT_FLOW"





AGENT_SYSTEM_PROMPT = """
You are an autonomous shopping assistant operating a real browser via tools.
Objective: Find an item on Amazon that matches the user's query and does NOT exceed the given max price (if any).
Then add it to cart and proceed to checkout, stopping before any actual purchase.
STRICT RULE: Never define new functions with the same names as tools.
Always call the registered @tool functions directly.

Store max_price as a variable first.
Print each steps' description for users.
If you failed to execute the function, call human_gate()

Steps:
- Prefer direct results via amazon_open_results(query, max_price).
- On a results page, use amazon_add_to_cart() without argument.
- To add to cart: call amazon_add_to_cart(). If you receive ADD_FAILED_NEEDS_HUMAN, call human_gate(), then retry add.
- To proceed to checkout: call amazon_proceed_to_checkout(). If it returns HUMAN_NEEDED_SIGNIN or HUMAN_NEEDED_CAPTCHA, call human_gate() and retry.
- NEVER place the order. Stop after reaching the checkout/payment stage.
- After calling amazon_proceed_to_checkout(), call amazon_stop_if_checkout_spc() and finish your action. 

amazon_open_results(query, max_price): Open Amazon search results for the query, optionally capped by a maximum price.
amazon_next_results_page(): Go to the next search results page if available.
amazon_open_product(): Open a product detail page
amazon_add_to_cart(): Click the “Add to Cart” button, retrying after variant selection if needed.
amazon_proceed_to_checkout(): Proceed from the cart to the checkout flow (may require sign-in or CAPTCHA).
close_popups(): Dismiss any visible modal or popup windows (e.g., warranty upsell, alerts).
amazon_stop_if_checkout_spc(close_browser=True): Stop the process if SPC checkout page is reached. After call this, finish your action.
go_back(): Navigate back to the previous page.
go_to(url): Navigate directly to a specified URL.
finish_session(): Close the browser session.
human_gate(message): Pause for required human action (e.g., login or CAPTCHA) before continuing.

Be concise in your tool usage. Between steps, rely on the page state (not assumptions).
Do NOT invent or assume any functions that are not in the registered tool list.
"""

@dataclass
class RunSpec:
    query: str
    max_price: Optional[float] = None
    pages: int = 5


def build_agent(max_steps: int = 10):
    model_id = "meta-llama/Llama-3.2-3B-Instruct" # "meta-llama/Llama-3.1-8B-Instruct"

    model = TransformersModel(model_id=model_id)

    agent = CodeAgent(
        tools=[
            go_to, go_back, finish_session, close_popups, search_item_ctrl_f,
            human_gate,
            amazon_open_results, amazon_next_results_page,
            amazon_open_product, 
            amazon_add_to_cart, amazon_proceed_to_checkout, amazon_stop_if_checkout_spc,
        ],
        model=model,
        max_steps=max_steps,
        verbosity_level=2,
        additional_authorized_imports=["helium"]
    )
    agent.python_executor("from helium import *")
    return agent

def parse_args():
    ap = argparse.ArgumentParser(description="Agentic Amazon shopper (query + max price)")
    ap.add_argument("--query", required=True, help="Product search query")
    ap.add_argument("--max", dest="max_price", type=float, default=None, help="Max acceptable price (USD)")
    ap.add_argument("--pages", type=int, default=5, help="Max results pages to traverse")
    ap.add_argument("--max-steps", type=int, default=10, help="Max tool-calling steps for the agent")
    return ap.parse_args()

def main():
    args = parse_args()
    spec = RunSpec(query=args.query, max_price=args.max_price, pages=args.pages)
    agent = build_agent(max_steps=args.max_steps)

    task = f"""
Find a product on Amazon for query: "{spec.query}" with max price: {spec.max_price}.
Search across up to {spec.pages} pages. Add a qualifying item to the cart and proceed to checkout,
stopping before placing any order. Use the tools as needed and human_gate() when blocked.
"""
    out = agent.run(task + AGENT_SYSTEM_PROMPT)
    print("\n=== FINAL OUTPUT ===")
    print(out)

if __name__ == "__main__":
    try:
        main()
    finally:
        pass
