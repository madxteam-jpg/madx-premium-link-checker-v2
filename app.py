import datetime
import io
import json
import time
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
import cloudscraper
from curl_cffi import requests as cffi_requests
from google import genai
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
# ReportLab PDF Libraries
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
import requests
import streamlit as st
import tldextract

# --- 1. SECURE API CONFIGURATION KEYS ---
AHREFS_API_KEY = st.secrets.get("AHREFS_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SCRAPERANT_KEY = st.secrets.get("SCRAPERANT_KEY", "")

# ClickUp Configuration
CLICKUP_API_TOKEN = st.secrets.get("CLICKUP_API_TOKEN", "")
CLICKUP_SPACE_ID = st.secrets.get("CLICKUP_SPACE_ID", "")
CLICKUP_CUSTOM_URL_FIELD_ID = st.secrets.get(
    "CLICKUP_CUSTOM_URL_FIELD_ID", "f06c29f4-d964-4719-a37d-b283ef19f48b"
)

CLICKUP_HEADERS = {"Authorization": CLICKUP_API_TOKEN}

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --- 2. URL & CLICKUP DATABASE HELPERS ---
def normalize_domain(url: str) -> str:
  """Extracts registered root domain (e.g., 'sub.example.co.uk' -> 'example.co.uk')."""
  if not url:
    return ""
  ext = tldextract.extract(url)
  if ext.domain and ext.suffix:
    return f"{ext.domain}.{ext.suffix}".lower()
  netloc = urlparse(url).netloc.lower()
  return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> str:
  """Normalizes URL by stripping trailing slashes, whitespace, and lowercasing."""
  if not url:
    return ""
  return url.strip().rstrip("/").lower()


@st.cache_data(ttl=300)
def fetch_clickup_database(space_id: str, custom_url_field_id: str):
  """Pulls tasks from ClickUp Space and parses the client identity from each task's parent folder."""
  if not CLICKUP_API_TOKEN or not space_id:
    return [], []

  url = f"https://api.clickup.com/api/v2/space/{space_id}/task"
  params = {"include_closed": "true", "subtasks": "true", "page": 0}

  records = []
  client_folders = set()

  while True:
    try:
      resp = requests.get(
          url, headers=CLICKUP_HEADERS, params=params, timeout=15
      )
      if resp.status_code != 200:
        break

      data = resp.json()
      tasks = data.get("tasks", [])
      if not tasks:
        break

      for task in tasks:
        folder_info = task.get("folder") or {}
        folder_name = folder_info.get("name")

        if not folder_name:
          continue

        folder_name = folder_name.strip()
        client_folders.add(folder_name)

        # Retrieve URL from Custom Field (handles string, dict, or nested payload)
        target_url = None
        if custom_url_field_id:
          for cf in task.get("custom_fields", []):
            if cf.get("id") == custom_url_field_id and cf.get("value"):
              val = cf["value"]
              if isinstance(val, dict):
                target_url = val.get("url") or val.get("value")
              else:
                target_url = str(val).strip()
              break

        # Fallback to description or task title if custom field was empty
        if not target_url and task.get("description"):
          for word in task["description"].split():
            if word.startswith(("http://", "https://")):
              target_url = word
              break

        if not target_url and task.get("name", "").startswith(
            ("http://", "https://")
        ):
          target_url = task["name"]

        if target_url:
          records.append({
              "task_id": task.get("id"),
              "task_name": task.get("name", "Untitled Task"),
              "task_url": task.get("url", "#"),
              "client_name": folder_name,
              "client_key": folder_name.lower(),
              "raw_url": target_url,
              "normalized_url": normalize_url(target_url),
              "domain": normalize_domain(target_url),
          })

      params["page"] += 1
    except Exception:
      break

  return records, sorted(list(client_folders))


def validate_clickup_duplicates(
    page_url: str, selected_client: str, records: list
):
  """Checks ClickUp for exact URL duplicates and domain collisions under the same client folder."""
  target_norm_url = normalize_url(page_url)
  target_domain = normalize_domain(page_url)
  selected_client_key = selected_client.strip().lower()

  exact_url_matches = []
  same_client_domain_matches = []

  for item in records:
    # Rule 1: Exact URL collision anywhere in ClickUp
    if item["normalized_url"] == target_norm_url:
      exact_url_matches.append(item)

    # Rule 2: Same root domain under the SAME client folder
    if (
        item["domain"] == target_domain
        and item["client_key"] == selected_client_key
    ):
      same_client_domain_matches.append(item)

  is_passed = (
      len(exact_url_matches) == 0 and len(same_client_domain_matches) == 0
  )

  return {
      "is_passed": is_passed,
      "target_domain": target_domain,
      "exact_url_matches": exact_url_matches,
      "same_client_domain_matches": same_client_domain_matches,
  }


# --- 3. CORE BACKEND HELPERS & SCRAPERS ---
def fetch_url_content(url):
  """Multi-stage scraper pipeline:

  1. ScrapingAnt via standard 'requests'
  2. Direct Chrome TLS impersonation via curl_cffi
  3. Cloudscraper engine
  4. Jina Reader mirror fallback
  """
  if SCRAPERANT_KEY:
    try:
      encoded_url = quote(url, safe="")
      api_endpoint = (
          f"https://api.scrapingant.com/v2/general"
          f"?x-api-key={SCRAPERANT_KEY}"
          f"&url={encoded_url}"
          f"&browser=true"
      )
      ant_response = requests.get(api_endpoint, timeout=60)

      if ant_response.status_code == 200:
        return ant_response
      elif ant_response.status_code in [401, 403]:
        st.error(
            "❌ **ScrapingAnt API Error:** Invalid API Key or out of credits."
        )
    except requests.exceptions.Timeout:
      st.warning(
          "⚠️ **ScrapingAnt Timed Out:** Falling back to secondary scrapers..."
      )
    except Exception as e:
      st.warning(f"⚠️ ScrapingAnt Connection Error: {e}")

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
      ),
      "Accept": (
          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
      ),
      "Accept-Language
