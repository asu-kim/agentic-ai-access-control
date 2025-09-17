#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, time
from typing import Any, Dict, List, Optional, Tuple
from io import BytesIO

from dotenv import load_dotenv
from PIL import Image

import helium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from smolagents import CodeAgent, tool, TransformersModel
from smolagents.agents import ActionStep as SmolActionStep

load_dotenv()
HEADLESS = False
FIXED_MODEL_ID = "meta-llama/Llama-3.1-8B-Instruct" # "meta-llama/Llama-3.1-8B-Instruct"

firefox_options = webdriver.FirefoxOptions()
if HEADLESS:
    firefox_options.add_argument("-headless")
firefox_options.set_preference("dom.webnotifications.enabled", False)
firefox_options.set_preference("dom.push.enabled", False)
firefox_options.set_preference("privacy.trackingprotection.enabled", True)

driver = helium.start_firefox(headless=HEADLESS, options=firefox_options)
driver.set_window_size(1280, 900)
driver.set_window_position(0, 0)

def _drv():
    d = helium.get_driver()
    if d is None:
        raise RuntimeError("Helium driver not initialized")
    return d

SELECTORS: Dict[str, List[Tuple[str, str]]] = {
    "COOKIE_ACCEPT": [
        ("xpath", '//button[.//span[contains(., "Accept")]]'),
        ("css", "button[aria-label*='Accept'][aria-label*='cookie']"),
    ],
    "DEST_INPUT": [
        ("css", "input[name='ss']"),
        ("css", "input[placeholder*='Where are you going']"),
        ("xpath", "//input[@name='ss']"),
    ],
    "DATE_CELL_START": [
        ("css", "span[data-testid='date-display-field-start']"),
        ("xpath", "//td[@data-date='{date}']"),
    ],
    "DATE_CELL_END": [
        ("css", "span[data-testid='date-display-field-end']"),
        ("xpath", "//td[@data-date='{date}']"),
    ],
    "SEARCH_SUBMIT": [
        ("css", "button[type='submit'][data-testid='searchbox-submit-button']"),
        ("xpath", "//button[contains(., 'Search')]"),
    ],
    "GUEST_TOGGLE": [
        ("css", "[data-testid='occupancy-config'] button"),
    ],
    "ADULTS_PLUS": [
        ("css", "button[aria-label*='Increase number of Adults']"),
    ],
    "ADULTS_MINUS": [
        ("css", "button[aria-label*='Decrease number of Adults']"),
    ],
    "ROOMS_PLUS": [
        ("css", "button[aria-label*='Increase number of Rooms']"),
    ],
    "FILTER_STARS_4PLUS": [
        ("xpath", "//*[contains(.,'4 stars')]/ancestor::label"),
    ],
    "RESULT_CARD": [
        ("css", "[data-testid='property-card']"),
    ],
    "FIRST_RESULT_LINK": [
        ("css", "[data-testid='title-link']"),
        ("xpath", "(//a[@data-testid='title-link'])[1]"),
    ],
       "RESERVE_CTA": [
        ("css", "span[class='bui-button__text']"),
        ("xpath", '//button[contains(.,"I\'ll reserve") or contains(.,"I’ll reserve")]'),
        ("xpath", '//button[contains(.,"Reserve")]'),
        ("xpath", '//button[contains(.,"Book now") or contains(.,"Continue")]'),

    ],
}

def _find(d, cands: List[Tuple[str, str]], **fmt):
    for how, sel in cands:
        try:
            s = sel.format(**fmt) if fmt else sel
        except KeyError:
            s = sel
        by = By.CSS_SELECTOR if how == "css" else By.XPATH
        els = d.find_elements(by, s)
        if els:
            return els[0]
    return None

def _click(d, cands: List[Tuple[str, str]], scroll=True, **fmt) -> bool:
    el = _find(d, cands, **fmt)
    if not el:
        return False
    if scroll:
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        d.execute_script("arguments[0].click();", el)
    except Exception:
        try:
            el.click()
        except Exception:
            return False
    return True

def _type(d, cands: List[Tuple[str, str]], text: str, clear=True) -> bool:
    el = _find(d, cands)
    if not el:
        return False
    try:
        if clear:
            el.clear()
    except Exception:
        pass
    el.send_keys(text)
    return True


@tool
def go_to(url: str) -> str:
    """Open a URL in the current browser tab.

    Args:
        url (str): The absolute or relative URL to open.

    Returns:
        str: A confirmation message with the navigated URL.
    """
    d = _drv()
    d.get(url)
    return f"Navigated: {url}"

@tool
def close_popups() -> str:
    """Close modal/pop-up windows by sending ESC multiple times.

    Returns:
        str: A message indicating the attempt to close popups.
    """
    import time
    d = _drv()
    chain = webdriver.ActionChains(d)
    for _ in range(3):
        chain.send_keys(Keys.ESCAPE).perform()
        time.sleep(0.15)
    _click(d, SELECTORS["COOKIE_ACCEPT"])
    return "Popups closed"

@tool
def bkg_home(lang: str = "en-us", currency: str = "USD") -> str:
    """Open Booking.com homepage with specific language and currency.

    Args:
        lang (str): Locale code (e.g., "en-us", "ko-kr").
        currency (str): Currency code (e.g., "USD", "KRW", "EUR").

    Returns:
        str: A confirmation message with the opened URL.
    """

    url = f"https://www.booking.com/?lang={lang}&selected_currency={currency}"
    return go_to(url)

    
@tool
def bkg_set_destination(city: str) -> str:
    """Fill the destination field and select the first autocomplete option.

    Args:
        city (str): Destination city or area (e.g., "Seoul", "San Francisco").

    Returns:
        str: A confirmation message with the chosen destination, 
             or an error note if input not found.
    """
    import time
    d = _drv()
    close_popups()
    ok = _click(d, SELECTORS["DEST_INPUT"])
    if not ok:
        return "Destination input not found."
    try:
        el = _find(d, SELECTORS["DEST_INPUT"])
        if el is None:
            return "Destination input not found."
        try:
            el.clear()
        except Exception:
            pass
        el.send_keys(city)
    except Exception:
        return "Typing failed."
    time.sleep(0.6)
    clicked = False
    cand_xpaths = [
        "//ul[@role='listbox']//li[.//div[contains(., $city)] or .//span[contains(., $city)] or .//button[contains(., $city)]]",
        "//div[@data-testid='autocomplete-results']//button[contains(normalize-space(.), $city)]",
        "//li[contains(@class,'autocomplete')]//button[contains(normalize-space(.), $city)]",
        "(//ul[@role='listbox']//li//button)[1]",
        "(//div[@data-testid='autocomplete-results']//button)[1]"
    ]
    for xp in cand_xpaths:
        try:
            elems = d.find_elements(By.XPATH, xp.replace("$city", city))
            if not elems:
                continue
            d.execute_script("arguments[0].scrollIntoView({block:'center'});", elems[0])
            try:
                d.execute_script("arguments[0].click();", elems[0])
            except Exception:
                elems[0].click()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        try:
            ActionChains(d).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
            clicked = True
        except Exception:
            pass
    time.sleep(0.5)
    try:
        val = _find(d, SELECTORS["DEST_INPUT"]).get_attribute("value") or ""
    except Exception:
        val = ""
    if not val or (city.lower() not in val.lower()):
        try:
            _click(d, SELECTORS["DEST_INPUT"])
            ActionChains(d).send_keys(Keys.CONTROL, "a").send_keys(Keys.DELETE).perform()
            _type(d, SELECTORS["DEST_INPUT"], city, clear=True)
            time.sleep(0.5)
            ActionChains(d).send_keys(Keys.ARROW_DOWN).send_keys(Keys.ENTER).perform()
            time.sleep(0.5)
            val = _find(d, SELECTORS["DEST_INPUT"]).get_attribute("value") or ""
        except Exception:
            pass
    return f"Destination set: {val or city}"


@tool
def bkg_set_dates(checkin: str, checkout: str) -> str:
    """Pick check-in and check-out dates from the calendar.

    Args:
        checkin (str): Check-in date in 'YYYY-MM-DD' format.
        checkout (str): Check-out date in 'YYYY-MM-DD' format.

    Returns:
        str: A status message indicating the selected dates.
    """
    d = _drv()
    close_popups()
    ok_in = _click(d, SELECTORS["DATE_CELL_START"], date=checkin)
    ok_out = _click(d, SELECTORS["DATE_CELL_END"], date=checkout)
    return f"Dates set: {checkin} → {checkout} ({ok_in},{ok_out})"

@tool
def bkg_set_guests(adults: int = 1, rooms: int = 1) -> str:
    """Open the guests widget and configure number of adults and rooms.

    Args:
        adults (int): Number of adults to set (>=1).
        rooms (int): Number of rooms to set (>=1).

    Returns:
        str: A confirmation message with the final guests/rooms counts.
    """
    import time
    d = _drv()
    close_popups()
    _click(d, SELECTORS["GUEST_TOGGLE"])
    time.sleep(0.2)
    for _ in range(5):
        _click(d, SELECTORS["ADULTS_MINUS"], scroll=False)
    for _ in range(max(adults,1)):
        _click(d, SELECTORS["ADULTS_PLUS"], scroll=False)
    for _ in range(max(rooms-1,0)):
        _click(d, SELECTORS["ROOMS_PLUS"], scroll=False)
    return f"Guests set: adults={adults}, rooms={rooms}"

@tool
def bkg_accept_cookies() -> str:
    """Accept Booking.com cookie banner if present.

    Returns:
        str: Whether the cookie accept button was clicked.
    """
    d = _drv()
    clicked = _click(d, SELECTORS["COOKIE_ACCEPT"])
    return "Cookie accept clicked." if clicked else "Cookie banner not found."

@tool
def bkg_submit_search() -> str:
    """
    Click the search submit button on the Booking.com search form.

    Returns:
        str: "Search submitted." if the button was clicked, otherwise a 'not found' message.
    """
    import time
    d = _drv()
    ok = _click(d, SELECTORS["SEARCH_SUBMIT"])
    time.sleep(1.0)
    return "Search submitted." if ok else "Search button not found."

@tool
def bkg_apply_star_filter(min_stars: int = 4) -> str:
    """Apply a minimum star rating filter (e.g., 4+ stars).

    Args:
        min_stars (int): Minimum star rating to apply (2, 3, 4, or 5).

    Returns:
        str: A message indicating which star filters were applied or if not found.
    """
    import time
    d = _drv()
    close_popups()
    if _click(d, SELECTORS["FILTER_STARS_4PLUS"]):
        time.sleep(0.4)
        return f"Star filter applied: >= {min_stars}"
    return "Star filter not found"

@tool
def bkg_open_first_result() -> str:
    """Open the first property card result.
    
    Returns:
        str: A message indicating whether it found the result or not.
    """
    d = _drv()
    close_popups()
    ok = _click(d, SELECTORS["FIRST_RESULT_LINK"])
    return "Opened first result." if ok else "No result link found."

@tool
def bkg_click_reserve_cta() -> str:
    """Click generic Reserve/Book/Continue CTAs on Booking.com flows.

    Returns:
        str: Whether a CTA was clicked.
    """
    import time
    d = _drv()
    if _click(d, SELECTORS["RESERVE_CTA"]):
        time.sleep(1.5)
        return "Clicked Reserve/Book/Continue CTA."
    return "Reserve/Continue CTA not found."


def build_agent(max_steps: int = 10) -> CodeAgent:
    model = TransformersModel(model_id=FIXED_MODEL_ID)
    tools = [
        go_to, close_popups,
        bkg_home, bkg_set_destination, bkg_set_dates, bkg_set_guests,
        bkg_submit_search, bkg_apply_star_filter, bkg_accept_cookies,
        bkg_open_first_result, bkg_click_reserve_cta
    ]
    agent = CodeAgent(
        tools=tools,
        model=model,
        additional_authorized_imports=["helium"],
        max_steps=max_steps,
        verbosity_level=2,
    )
    agent.python_executor("from helium import *")
    return agent

SYSTEM_GUIDE = """
You are a browser-automation AI. Choose and call TOOLS in any order to achieve the goal.
Your job: translate natural-language user goals into precise sequences of tool calls to set location, dates, guests, filters, and sorting; then open a relevant result.

Print each steps' description for users. 

Planning rules:
1) Always start from bkg_home(lang,currency).
2) City: bkg_set_destination(city). If autocomplete fails, try again.
3) Dates: bkg_set_dates(checkin, checkout) using YYYY-MM-DD strings.
4) Guests/rooms: bkg_set_guests(adults, rooms).
5) Submit: bkg_submit_search(), then wait briefly.
6) Filters:
   - If user requests "4-star or higher", call bkg_apply_star_filter(min_stars=4).
   - If user requests price cap, prefer sorting by price and selecting cheaper ones later.
7) Sorting: 'top_reviewed' when appropriate.
8) bkg_open_first_result()
9) Use bkg_click_reserve_cta() on current tap - first_result tap that you opend in the step 8.


Output: Use the available tools only. No code execution outside tools.

Booking.com specific tools you can use:
- bkg_home(lang, currency): open Booking.com homepage with language and currency set
- bkg_click_reserve_cta(): navigate to reservation page.
- bkg_set_destination(city): fill the destination search box with a city/area
- bkg_set_dates(checkin, checkout): select check-in and check-out dates in calendar
- bkg_set_guests(adults, rooms): configure number of adults and rooms
- bkg_submit_search(): click the search button to start a search
- bkg_apply_star_filter(min_stars): apply a minimum star rating filter (e.g., 4+)
- bkg_accept_cookies(): accept the cookie consent banner if present
- bkg_open_first_result(): open the first property result link from the results page

General tools:
- go_to(url), close_popups()

Rules:
- After each action, observe page changes before issuing the next one.
- Prefer bkg_accept_cookies() instead of trying to click X on banners.
- Do NOT type or submit sensitive information unless explicitly instructed.
- STRICT RULE: Never define new functions with the same names as tools.
Always call the registered @tool functions directly.

"""

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Booking.com search agent")
    parser.add_argument("--query", type=str, required=True)
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()

    agent = build_agent(max_steps=args.max_steps)
    request = f"{args.query}\n\n{SYSTEM_GUIDE}"
    out = agent.run(request)
    print("\n=== Final output ===")
    print(out)
