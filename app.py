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
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", "")
SCRAPERANT_KEY = st.secrets.get("SCRAPERANT_KEY", "")

# ClickUp Configuration
CLICKUP_API_TOKEN = st.secrets.get("CLICKUP_API_TOKEN", "")
CLICKUP_TEAM_ID = st.secrets.get("CLICKUP_TEAM_ID", "90152316361")
CLICKUP_SPACE_ID = st.secrets.get("CLICKUP_SPACE_ID", "901510491615")
CLICKUP_CUSTOM_URL_FIELD_ID = st.secrets.get(
    "CLICKUP_CUSTOM_URL_FIELD_ID", "f06c29f4-d964-4719-a37d-b283ef19f48b"
)

CLICKUP_HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
}

# Initialize Gemini Client with 20-second timeout
gemini_client = (
    genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"timeout": 20000},
    )
    if GEMINI_API_KEY
    else None
)


# --- 2. URL & STRICT CLICKUP DATABASE HELPERS ---
def normalize_domain(url: str) -> str:
  """Extracts registered root domain (e.g. 'sub.example.co.uk' -> 'example.co.uk')."""
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
  """Crawls Space strictly via Folder hierarchy."""
  if not CLICKUP_API_TOKEN:
    return []

  records = []

  folders_url = f"https://api.clickup.com/api/v2/space/{space_id}/folder?archived=false"
  try:
    f_resp = requests.get(folders_url, headers=CLICKUP_HEADERS, timeout=15)
    if f_resp.status_code != 200:
      st.error(
          f"ClickUp Folder Fetch Error ({f_resp.status_code}): {f_resp.text}"
      )
      return []
    folders = f_resp.json().get("folders", [])
  except Exception as e:
    st.error(f"Exception fetching ClickUp folders: {e}")
    return []

  for folder in folders:
    folder_id = folder.get("id")
    folder_name = folder.get("name", "").strip()
    if not folder_name:
      continue

    lists = folder.get("lists", [])

    for lst in lists:
      list_id = lst.get("id")
      page = 0

      while True:
        tasks_url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
        params = {"include_closed": "true", "subtasks": "true", "page": page}
        try:
          t_resp = requests.get(
              tasks_url, headers=CLICKUP_HEADERS, params=params, timeout=15
          )
          if t_resp.status_code != 200:
            break

          data = t_resp.json()
          tasks = data.get("tasks", [])
          if not tasks:
            break

          for task in tasks:
            target_url = None
            for cf in task.get("custom_fields", []):
              if cf.get("id") == custom_url_field_id:
                val = cf.get("value")
                if isinstance(val, dict):
                  target_url = val.get("url") or val.get("value")
                elif val:
                  target_url = str(val).strip()
                break

            if target_url:
              task_identifier = task.get("custom_id") or task.get("id")
              records.append({
                  "task_id": task_identifier,
                  "task_name": task.get("name", "Untitled Task"),
                  "task_url": task.get(
                      "url", f"https://app.clickup.com/t/{task_identifier}"
                  ),
                  "folder_id": folder_id,
                  "client_folder": folder_name,
                  "client_key": folder_name.lower(),
                  "raw_url": target_url,
                  "normalized_url": normalize_url(target_url),
                  "domain": normalize_domain(target_url),
              })

          page += 1
          if page > 50:
            break
        except Exception:
          break

  return records


def validate_clickup_duplicates(page_url: str, records: list):
  """Scopes checks to the specific client folder owning the task."""
  target_norm_url = normalize_url(page_url)
  target_domain = normalize_domain(page_url)

  matching_url_records = [
      r for r in records if r["normalized_url"] == target_norm_url
  ]

  if not matching_url_records:
    return {
        "is_passed": False,
        "is_url_valid": False,
        "is_domain_valid": False,
        "url_count": 0,
        "domain_count": 0,
        "detected_client": "Not Found in ClickUp",
        "detected_folder_id": "N/A",
        "target_domain": target_domain,
        "client_url_matches": [],
        "same_client_domain_matches": [],
    }

  primary_task = matching_url_records[0]
  detected_client_folder = primary_task["client_folder"]
  detected_folder_id = primary_task.get("folder_id", "N/A")
  client_key = primary_task["client_key"]

  client_url_matches = [
      r for r in matching_url_records if r["client_key"] == client_key
  ]
  url_count = len(client_url_matches)

  same_client_domain_matches = [
      r
      for r in records
      if r["domain"] == target_domain and r["client_key"] == client_key
  ]
  domain_count = len(same_client_domain_matches)

  is_url_valid = url_count == 1
  is_domain_valid = domain_count == 1
  is_passed = is_url_valid and is_domain_valid

  return {
      "is_passed": is_passed,
      "is_url_valid": is_url_valid,
      "is_domain_valid": is_domain_valid,
      "url_count": url_count,
      "domain_count": domain_count,
      "detected_client": detected_client_folder,
      "detected_folder_id": detected_folder_id,
      "target_domain": target_domain,
      "client_url_matches": client_url_matches,
      "same_client_domain_matches": same_client_domain_matches,
  }


# --- 3. CORE BACKEND HELPERS & SCRAPERS ---
def fetch_url_content(url):
  """Multi-stage scraper pipeline."""
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
    except Exception:
      pass

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/124.0.0.0 Safari/537.36"
      ),
      "Accept": (
          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
      ),
      "Accept-Language": "en-US,en;q=0.9",
  }
  try:
    response = cffi_requests.get(
        url,
        impersonate="chrome124",
        headers=headers,
        timeout=10,
        allow_redirects=True,
    )
    if response.status_code == 200:
      return response
  except Exception:
    pass

  try:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    cs_response = scraper.get(url, timeout=12)
    if cs_response.status_code == 200:
      return cs_response
  except Exception:
    pass

  try:
    jina_url = f"https://r.jina.ai/{url}"
    jina_res = requests.get(jina_url, timeout=15)
    if jina_res.status_code == 200:
      return jina_res
  except Exception:
    pass

  return None


def get_domain_from_url(url):
  """Extracts the root domain from any URL string."""
  try:
    parsed_domain = urlparse(url).netloc
    if parsed_domain.startswith("www."):
      parsed_domain = parsed_domain[4:]
    return parsed_domain
  except Exception:
    return None


def check_link_and_tags(page_url, target_url, expected_anchor, brand_name):
  """Local Scraper: Audits redirects, UGC indicators, listicle position, and backlink tags."""
  results = {
      "link_found": False,
      "anchor_matches": False,
      "is_follow": True,
      "rel_tags": [],
      "brand_mentioned": False,
      "is_indexable": True,
      "is_ugc": False,
      "ugc_reason": "",
      "ugc_ai_verified": False,
      "ugc_ai_explanation": "",
      "link_context_snippet": "",
      "is_redirecting": False,
      "final_destination_url": page_url,
      "listicle_top_3_pass": "N/A",
      "html_content": "",
      "error": None,
  }

  response = fetch_url_content(page_url)
  if not response:
    results["error"] = (
        "Network Failure: Unable to connect to target domain across all"
        " scrapers."
    )
    return results

  if response.status_code != 200:
    results["error"] = f"Scrape Error: Status Code {response.status_code}"
    return results

  results["html_content"] = response.text

  if hasattr(response, "history") and len(response.history) > 0:
    results["is_redirecting"] = True
    results["final_destination_url"] = response.url

  soup = BeautifulSoup(response.text, "html.parser")
  page_text = soup.get_text().lower()

  if brand_name and brand_name.lower() in page_text:
    results["brand_mentioned"] = True

  robots_meta = soup.find("meta", attrs={"name": "robots"})
  if robots_meta and "noindex" in robots_meta.get("content", "").lower():
    results["is_indexable"] = False

  ugc_containers = soup.find_all(["div", "section", "ul", "ol"], class_=True)
  ugc_classes = {
      "comment-list",
      "comment-body",
      "comments-area",
      "forum-table",
      "vbulletin",
      "disqus_thread",
      "bbpress-forums",
  }
  ugc_text_patterns = [
      "leave a comment",
      "post a comment",
      "reply to this",
      "anonymous user",
  ]

  for element in ugc_containers:
    element_classes = set(" ".join(element.get("class", [])).lower().split())
    matched_classes = element_classes.intersection(ugc_classes)
    if matched_classes:
      results["is_ugc"] = True
      results["ugc_reason"] = (
          f"UGC markup container detected ({', '.join(matched_classes)})."
      )
      break

  if not results["is_ugc"] and any(
      phrase in page_text for phrase in ugc_text_patterns
  ):
    results["is_ugc"] = True
    results["ugc_reason"] = "User-Generated comment text patterns observed."

  target_clean = target_url.strip().lower()
  expected_anchor_clean = (
      expected_anchor.strip().lower() if expected_anchor else ""
  )

  links = soup.find_all("a", href=True)
  for link in links:
    if target_clean in link["href"].lower():
      results["link_found"] = True
      if expected_anchor_clean and expected_anchor_clean in link.text.lower():
        results["anchor_matches"] = True

      rel = link.get("rel", [])
      if isinstance(rel, str):
        rel = rel.split()
      results["rel_tags"] = rel

      if any(tag in ["nofollow", "sponsored", "ugc"] for tag in rel):
        results["is_follow"] = False

      # Capture surrounding parent HTML/text context for AI UGC double-checking
      parent_node = link.find_parent(["p", "li", "div", "article", "section"])
      if parent_node:
        ancestors = [
            f"{p.name}(class={p.get('class')}, id={p.get('id')})"
            for p in link.parents
            if p.name in ["article", "section", "div", "main", "aside", "footer"]
        ][:4]
        snippet_text = " ".join(parent_node.get_text().split())[:600]
        results["link_context_snippet"] = (
            f"DOM Ancestry: {' > '.join(ancestors)}\n"
            f"Anchor HTML: {str(link)}\n"
            f"Surrounding Paragraph Text: {snippet_text}"
        )
      else:
        results["link_context_snippet"] = f"Anchor HTML: {str(link)}"
      break

  page_title = soup.title.text.lower() if soup.title else ""
  listicle_triggers = [
      "best",
      "top",
      "tools",
      "ways",
      "apps",
      "platforms",
      "services",
  ]
  is_listicle = any(char.isdigit() for char in page_title) and any(
      w in page_title for w in listicle_triggers
  )

  if is_listicle and brand_name:
    headings = [
        h.text.strip().lower() for h in soup.find_all(["h1", "h2", "h3"])
    ][:3]
    brand_in_top_3 = any(brand_name.lower() in heading for heading in headings)
    results["listicle_top_3_pass"] = "PASS" if brand_in_top_3 else "FAIL"

  return results


# --- 4. AI RELEVANCY & UGC DOUBLE-CHECKER (GEMINI + GROQ FALLBACK) ---
def _call_groq_relevancy_fallback(prompt: str) -> dict:
  """Secondary AI provider fallback via Groq's OpenAI-compatible Chat Completions API."""
  if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not configured in secrets.")

  groq_url = "https://api.groq.com/openai/v1/chat/completions"
  headers = {
      "Authorization": f"Bearer {GROQ_API_KEY}",
      "Content-Type": "application/json",
  }

  system_instruction = (
      "You are an expert SEO Quality Assurance Auditor. Respond ONLY with a valid"
      " JSON object containing exactly five keys: 'niche_pass' ('PASS' or"
      " 'FAIL'), 'topic_pass' ('PASS' or 'FAIL'), 'reason' (string),"
      " 'is_actual_ugc' (boolean: true ONLY if the backlink itself is placed"
      " inside a user comment, forum post, or unmoderated profile; false if it"
      " is inside the main editorial article body even if a comment box exists"
      " elsewhere on the page), and 'ugc_audit_reason' (concise string"
      " explaining where the link sits)."
  )

  groq_models = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
  last_groq_error = ""

  for model_name in groq_models:
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    try:
      resp = requests.post(
          groq_url, headers=headers, json=payload, timeout=20
      )
      if resp.status_code == 200:
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        niche_val = str(parsed.get("niche_pass", "FAIL")).upper()
        topic_val = str(parsed.get("topic_pass", "FAIL")).upper()
        return {
            "niche_pass": niche_val if niche_val in ["PASS", "FAIL"] else "FAIL",
            "topic_pass": topic_val if topic_val in ["PASS", "FAIL"] else "FAIL",
            "reason": (
                f"[Evaluated via Groq Fallback: {model_name}]"
                f" {parsed.get('reason', 'No reason provided.')}"
            ),
            "is_actual_ugc": bool(parsed.get("is_actual_ugc", False)),
            "ugc_audit_reason": str(
                parsed.get("ugc_audit_reason", "Verified by Groq AI.")
            ),
        }
      else:
        last_groq_error = f"HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
      last_groq_error = str(e)

  raise RuntimeError(f"Groq fallback failed: {last_groq_error}")


def analyze_relevancy_and_ugc_with_ai(
    page_html, target_niche, business_topic, qa_results
):
  """Evaluates domain niche/topic alignment AND double-checks UGC placement flags.

  1. Rotates across available Gemini models (3.8 Flash -> 2.5 Flash -> 2.5 Flash Lite -> 3.6 Flash).
  2. Automatically falls back to Groq if Gemini hits rate limits or errors.
  """
  default_ugc = qa_results.get("is_ugc", False)
  default_ugc_reason = qa_results.get("ugc_reason", "")

  if not gemini_client and not GROQ_API_KEY:
    return {
        "niche_pass": "Error",
        "topic_pass": "Error",
        "reason": "Both GEMINI_API_KEY and GROQ_API_KEY are missing.",
        "is_actual_ugc": default_ugc,
        "ugc_audit_reason": default_ugc_reason,
    }
  if not page_html:
    return {
        "niche_pass": "Error",
        "topic_pass": "Error",
        "reason": "No HTML content was retrieved.",
        "is_actual_ugc": default_ugc,
        "ugc_audit_reason": default_ugc_reason,
    }

  soup = BeautifulSoup(page_html, "html.parser")
  for script in soup(["script", "style", "nav", "footer", "header"]):
    script.decompose()

  pure_text = soup.get_text(separator=" ")
  truncated_text = " ".join(pure_text.split()[:1500])

  link_snippet = qa_results.get(
      "link_context_snippet", "Target link node was not isolated in DOM."
  )
  dom_ugc_flag = (
      f"Flagged ({default_ugc_reason})" if default_ugc else "Clean (No UGC DOM flags)"
  )

  prompt = f"""
    You are an expert SEO Quality Assurance Auditor. Analyze this page to evaluate relevance AND verify whether the backlink placement is genuine editorial content or User-Generated Content (UGC).

    Target Niche/Industry: {target_niche}
    Client Business Topic/Core Product: {business_topic}

    --- BACKLINK PLACEMENT CONTEXT ---
    Automated DOM Scraper UGC Status: {dom_ugc_flag}
    {link_snippet}

    --- PAGE CONTENT (TRUNCATED) ---
    \"\"\"{truncated_text}\"\"\"

    Evaluate:
    1. Niche Relevancy ('niche_pass'): Is this page contextually adjacent or relevant to '{target_niche}'?
    2. Topic Relevancy ('topic_pass'): Does this theme make semantic sense to mention '{business_topic}'?
    3. UGC Placement Audit ('is_actual_ugc'): Many standard blog templates include a 'comments-area' or 'leave a comment' box at the bottom of the page even when the article itself is editorial. Set 'is_actual_ugc' to TRUE ONLY if the target backlink itself is inside a reader comment, forum thread, or unmoderated user profile. Set 'is_actual_ugc' to FALSE if the backlink is placed inside the main editorial article body or listicle.
    4. Provide a brief 'ugc_audit_reason' explaining whether the link is in the editorial body or a comment section.
    """

  gemini_error = "Gemini API Key not configured."

  if gemini_client:
    candidate_models = [
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.6-flash",
    ]
    for model_id in candidate_models:
      try:
        response = gemini_client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "niche_pass": {"type": "STRING", "enum": ["PASS", "FAIL"]},
                        "topic_pass": {"type": "STRING", "enum": ["PASS", "FAIL"]},
                        "reason": {"type": "STRING"},
                        "is_actual_ugc": {"type": "BOOLEAN"},
                        "ugc_audit_reason": {"type": "STRING"},
                    },
                    "required": [
                        "niche_pass",
                        "topic_pass",
                        "reason",
                        "is_actual_ugc",
                        "ugc_audit_reason",
                    ],
                },
                temperature=0.1,
            ),
        )
        result = json.loads(response.text)
        result["reason"] = f"[Evaluated via Gemini: {model_id}] {result['reason']}"
        result["ugc_audit_reason"] = (
            f"[{model_id}] {result['ugc_audit_reason']}"
        )
        return result
      except Exception as e:
        gemini_error = str(e)
        continue

  if GROQ_API_KEY:
    try:
      return _call_groq_relevancy_fallback(prompt)
    except Exception as groq_err:
      return {
          "niche_pass": "Error",
          "topic_pass": "Error",
          "reason": (
              f"Primary (Gemini) failed: {gemini_error} | Secondary (Groq)"
              f" failed: {groq_err}"
          ),
          "is_actual_ugc": default_ugc,
          "ugc_audit_reason": default_ugc_reason,
      }

  return {
      "niche_pass": "Error",
      "topic_pass": "Error",
      "reason": f"Gemini Exception (and no GROQ_API_KEY set): {gemini_error}",
      "is_actual_ugc": default_ugc,
      "ugc_audit_reason": default_ugc_reason,
  }


# --- 5. ADVANCED AHREFS SITEWIDE ENGINE ---
def fetch_advanced_ahrefs_data(target_url):
  domain = get_domain_from_url(target_url)
  results = {"dr": "N/A", "traffic_history": None, "keywords": [], "error": ""}

  if not domain:
    results["error"] = "Invalid target domain format."
    return results
  if not AHREFS_API_KEY:
    results["error"] = "Ahrefs API key configuration is missing."
    return results

  headers = {
      "Authorization": f"Bearer {AHREFS_API_KEY}",
      "Accept": "application/json",
  }
  today = datetime.date.today()
  yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
  six_months_ago = (today - datetime.timedelta(days=180)).strftime("%Y-%m-%d")

  try:
    res = requests.get(
        "https://api.ahrefs.com/v3/site-explorer/domain-rating",
        headers=headers,
        params={"target": domain, "date": yesterday_str, "output": "json"},
        timeout=10,
    )
    if res.status_code == 200:
      results["dr"] = res.json().get("domain_rating", {}).get(
          "domain_rating", "N/A"
      )
  except Exception:
    pass

  time.sleep(1.0)

  try:
    res = requests.get(
        "https://api.ahrefs.com/v3/site-explorer/metrics-history",
        headers=headers,
        params={
            "target": domain,
            "mode": "subdomains",
            "date_from": six_months_ago,
            "date_to": yesterday_str,
            "history_grouping": "monthly",
            "output": "json",
        },
        timeout=10,
    )
    if res.status_code == 200:
      raw = res.json().get("metrics", [])
      results["traffic_history"] = sorted(raw, key=lambda x: x.get("date", ""))
  except Exception:
    pass

  return results


# --- 6. PDF REPORT GENERATOR ENGINE ---
def generate_pdf_report(
    page_url,
    target_url,
    brand_name,
    anchor_text,
    client_name,
    folder_id,
    clickup_check,
    qa_results,
    ahrefs_results,
    ai_relevancy,
):
  """Generates a downloadable PDF report summarizing all 4 audit sections."""
  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
      buffer,
      pagesize=letter,
      rightMargin=36,
      leftMargin=36,
      topMargin=36,
      bottomMargin=36,
  )
  styles = getSampleStyleSheet()

  title_style = ParagraphStyle(
      "ReportTitle",
      parent=styles["Heading1"],
      fontSize=20,
      leading=24,
      textColor=colors.HexColor("#1E293B"),
  )
  subtitle_style = ParagraphStyle(
      "ReportSubtitle",
      parent=styles["Normal"],
      fontSize=10,
      leading=14,
      textColor=colors.HexColor("#64748B"),
  )
  heading_style = ParagraphStyle(
      "SectionHeading",
      parent=styles["Heading2"],
      fontSize=13,
      leading=16,
      textColor=colors.HexColor("#0F172A"),
      spaceBefore=12,
      spaceAfter=6,
  )
  body_style = ParagraphStyle(
      "BodyTextCustom",
      parent=styles["Normal"],
      fontSize=9,
      leading=12,
      textColor=colors.HexColor("#334155"),
  )
  badge_pass = ParagraphStyle(
      "PassBadge",
      parent=body_style,
      textColor=colors.HexColor("#166534"),
      fontName="Helvetica-Bold",
  )
  badge_fail = ParagraphStyle(
      "FailBadge",
      parent=body_style,
      textColor=colors.HexColor("#991B1B"),
      fontName="Helvetica-Bold",
  )

  elements = []

  # Header
  elements.append(
      Paragraph("<b>Enterprise Link Building QA Audit</b>", title_style)
  )
  elements.append(
      Paragraph(
          "Generated on:"
          f" {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC",
          subtitle_style,
      )
  )
  elements.append(Spacer(1, 10))
  elements.append(
      HRFlowable(
          width="100%",
          thickness=1,
          color=colors.HexColor("#CBD5E1"),
          spaceAfter=15,
      )
  )

  # Section 1: Specs & Summary
  elements.append(
      Paragraph(
          "1. Audit Input Specifications & High-Level Summary", heading_style
      )
  )
  spec_data = [
      [
          Paragraph("<b>Auto-Detected Client Folder:</b>", body_style),
          Paragraph(f"{client_name} (ID: {folder_id})", body_style),
      ],
      [
          Paragraph("<b>Live Page URL:</b>", body_style),
          Paragraph(page_url, body_style),
      ],
      [
          Paragraph("<b>Target Landing URL:</b>", body_style),
          Paragraph(target_url, body_style),
      ],
      [
          Paragraph("<b>Customer Brand Name:</b>", body_style),
          Paragraph(brand_name or "N/A", body_style),
      ],
      [
          Paragraph("<b>Expected Anchor:</b>", body_style),
          Paragraph(anchor_text or "N/A", body_style),
      ],
  ]
  spec_table = Table(spec_data, colWidths=[160, 380])
  spec_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
          ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
          ("PADDING", (0, 0), (-1, -1), 5),
      ])
  )
  elements.append(spec_table)
  elements.append(Spacer(1, 10))

  summary_metrics = [
      [
          Paragraph("<b>ClickUp Uniqueness</b>", body_style),
          Paragraph("<b>Domain Rating</b>", body_style),
          Paragraph("<b>Crawler Index Status</b>", body_style),
      ],
      [
          Paragraph(
              "PASS (Unique)"
              if clickup_check.get("is_passed")
              else "FAIL (Validation Error)",
              badge_pass if clickup_check.get("is_passed") else badge_fail,
          ),
          Paragraph(f"DR {ahrefs_results.get('dr', 'N/A')}", body_style),
          Paragraph(
              "PASS (Indexable)"
              if qa_results.get("is_indexable")
              else "FAIL (NoIndex)",
              badge_pass if qa_results.get("is_indexable") else badge_fail,
          ),
      ],
  ]
  summary_table = Table(summary_metrics, colWidths=[180, 180, 180])
  summary_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
          ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
          ("ALIGN", (0, 0), (-1, -1), "CENTER"),
          ("PADDING", (0, 0), (-1, -1), 6),
      ])
  )
  elements.append(summary_table)
  elements.append(Spacer(1, 15))

  # Section 2: ClickUp Conflict Verification
  elements.append(
      Paragraph("2. ClickUp Database Uniqueness Audit", heading_style)
  )

  url_status_label = "PASS" if clickup_check["is_url_valid"] else "FAIL"
  if clickup_check["url_count"] == 0:
    url_detail_text = (
        "Not found in ClickUp Published URL field (0 occurrences). Task must be"
        " logged prior to QA."
    )
  elif clickup_check["url_count"] == 1:
    url_detail_text = (
        f"Verified: Exists exactly once in folder '{client_name}'."
    )
  else:
    url_detail_text = (
        f"Duplicate Conflict: Found {clickup_check['url_count']} times inside"
        f" folder '{client_name}'."
    )

  domain_status_label = "PASS" if clickup_check["is_domain_valid"] else "FAIL"
  if clickup_check["domain_count"] == 0:
    domain_detail_text = (
        f"Domain '{clickup_check['target_domain']}' not found under"
        f" {client_name} (0 occurrences)."
    )
  elif clickup_check["domain_count"] == 1:
    domain_detail_text = (
        f"Verified: Domain '{clickup_check['target_domain']}' is unique for"
        f" folder '{client_name}'."
    )
  else:
    domain_detail_text = (
        f"Duplicate Conflict: Domain '{clickup_check['target_domain']}' appears"
        f" {clickup_check['domain_count']} times under folder '{client_name}'."
    )

  clickup_rows = [
      ["Check Criterion", "Status", "Details / Diagnostics"],
      [
          f"Folder URL Count ({client_name})",
          url_status_label,
          url_detail_text,
      ],
      [
          f"Folder Domain Count ({client_name})",
          domain_status_label,
          domain_detail_text,
      ],
  ]

  clickup_table_data = []
  for row in clickup_rows:
    status_p = (
        Paragraph(
            f"<b>{row[1]}</b>",
            badge_pass if row[1] == "PASS" else badge_fail,
        )
        if row[1] in ["PASS", "FAIL"]
        else Paragraph(f"<b>{row[1]}</b>", body_style)
    )
    clickup_table_data.append(
        [Paragraph(row[0], body_style), status_p, Paragraph(row[2], body_style)]
    )

  clickup_table = Table(clickup_table_data, colWidths=[160, 80, 300])
  clickup_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
          ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
          ("PADDING", (0, 0), (-1, -1), 5),
      ])
  )
  elements.append(clickup_table)
  elements.append(Spacer(1, 15))

  # Section 3: Technical Compliance
  elements.append(
      Paragraph("3. Technical Placement & Compliance Audit", heading_style)
  )
  tech_checks = [
      ["Check Item", "Status", "Details / Diagnostics"],
      [
          "Redirect Inspection",
          "WARNING" if qa_results.get("is_redirecting") else "PASS",
          f"Destination: {qa_results.get('final_destination_url')}",
      ],
      [
          "UGC Structural Check",
          "FAIL" if qa_results.get("is_ugc") else "PASS",
          qa_results.get("ugc_reason") or "Clean article layout.",
      ],
      [
          "Backlink Node Check",
          "PASS" if qa_results.get("link_found") else "FAIL",
          (
              "Target link source anchor discovered."
              if qa_results.get("link_found")
              else "Target link missing from HTML source."
          ),
      ],
      [
          "Anchor Text Alignment",
          "PASS" if qa_results.get("anchor_matches") else "FAIL",
          f"Expected: '{anchor_text}'",
      ],
      [
          "Link Follow Attribution",
          "PASS" if qa_results.get("is_follow") else "RESTRICTED",
          f"Rel attributes: {qa_results.get('rel_tags')}"
          if qa_results.get("rel_tags")
          else "DoFollow",
      ],
  ]

  tech_table_data = []
  for row in tech_checks:
    status_p = (
        Paragraph(
            f"<b>{row[1]}</b>",
            badge_pass if row[1] == "PASS" else badge_fail,
        )
        if row[1] in ["PASS", "FAIL", "WARNING", "RESTRICTED"]
        else Paragraph(f"<b>{row[1]}</b>", body_style)
    )
    tech_table_data.append(
        [Paragraph(row[0], body_style), status_p, Paragraph(row[2], body_style)]
    )

  tech_table = Table(tech_table_data, colWidths=[150, 90, 300])
  tech_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
          ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
          ("PADDING", (0, 0), (-1, -1), 5),
      ])
  )
  elements.append(tech_table)
  elements.append(Spacer(1, 15))

  # Section 4: Semantic AI
  elements.append(
      Paragraph(
          "4. Semantic AI Relevancy Audit (AI Evaluation)", heading_style
      )
  )
  ai_data = [
      [
          Paragraph("<b>Niche Relevancy:</b>", body_style),
          Paragraph(
              ai_relevancy.get("niche_pass", "N/A"),
              badge_pass
              if ai_relevancy.get("niche_pass") == "PASS"
              else badge_fail,
          ),
      ],
      [
          Paragraph("<b>Topic Alignment:</b>", body_style),
          Paragraph(
              ai_relevancy.get("topic_pass", "N/A"),
              badge_pass
              if ai_relevancy.get("topic_pass") == "PASS"
              else badge_fail,
          ),
      ],
      [
          Paragraph("<b>AI Reason Log:</b>", body_style),
          Paragraph(ai_relevancy.get("reason", "N/A"), body_style),
      ],
  ]
  ai_table = Table(ai_data, colWidths=[120, 420])
  ai_table.setStyle(
      TableStyle([
          ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
          ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
          ("PADDING", (0, 0), (-1, -1), 6),
      ])
  )
  elements.append(ai_table)

  doc.build(elements)
  buffer.seek(0)
  return buffer


# --- 7. STREAMLIT FRONT-END DASHBOARD UI ---
st.set_page_config(
    page_title="Enterprise Link Building QA", page_icon="🔗", layout="wide"
)

# Header Row with Manual Refresh Option
head_col1, head_col2 = st.columns([4, 1])
with head_col1:
  st.title("🔗 MADX Link Building QA")
with head_col2:
  st.write("")
  if st.button("🔄 Refresh ClickUp Cache", use_container_width=True):
    fetch_clickup_database.clear()
    st.toast("ClickUp database cache refreshed!", icon="🧹")

# Pre-fetch ClickUp database (cached 5 min)
with st.spinner(
    "Syncing ClickUp Workspace (traversing client folders & custom fields)..."
):
  clickup_records = fetch_clickup_database(
      CLICKUP_SPACE_ID, CLICKUP_CUSTOM_URL_FIELD_ID
  )

with st.form("qa_form"):
  st.subheader("📋 Input Specifications")
  st.caption(
      "💡 *Client folder name and folder ID are auto-detected from ClickUp"
      " using the live page URL.*"
  )

  col1, col2 = st.columns(2)
  with col1:
    page_url = st.text_input(
        "Live Page URL (Where linkbuilder placed the link)",
        placeholder="https://scrape.do/blog/sample-article",
    )
    target_url = st.text_input(
        "Target URL (Client Landing Page)", placeholder="https://client.com/demo"
    )
    brand_name = st.text_input(
        "Customer Brand Name", placeholder="e.g. Scrape.do"
    )
  with col2:
    anchor_text = st.text_input(
        "Expected Anchor Text", placeholder="e.g. web scraping api"
    )
    target_niche = st.text_input(
        "Target Niche / Industry", placeholder="e.g. Web Scraping, Proxy Tools"
    )
    business_topic = st.text_input(
        "Client Business Topic / Core Product",
        placeholder="e.g. Automated Data Extraction Software",
    )

  submitted = st.form_submit_button("Execute Full System QA Audit")


# --- 8. UNIFIED FORM SUBMISSION & ALL-IN-ONE REPORT VIEW ---
if submitted:
  if not page_url or not target_url:
    st.error(
        "❌ Form Incomplete: Please provide both the Live Page URL and Target"
        " URL."
    )
  else:
    # STEP 1: ClickUp Database Conflict Check
    with st.spinner(
        "Step 1/4: Resolving ClickUp client folder & auditing uniqueness..."
    ):
      clickup_check = validate_clickup_duplicates(page_url, clickup_records)
      detected_client = clickup_check["detected_client"]
      detected_folder_id = clickup_check["detected_folder_id"]

    # STEP 2: Live HTML Scrape & DOM Parsing
    with st.spinner("Step 2/4: Scraping live page code frameworks..."):
      qa_results = check_link_and_tags(
          page_url, target_url, anchor_text, brand_name
      )

    # STEP 3: Ahrefs Metrics
    with st.spinner(
        "Step 3/4: Fetching sitewide authority metrics from Ahrefs v3..."
    ):
      ahrefs_results = fetch_advanced_ahrefs_data(page_url)

    # STEP 4: Semantic AI Relevancy & UGC Double-Check (Gemini + Groq Fallback)
    with st.spinner(
        "Step 4/4: Running contextual relevancy & UGC placement verification"
        " (Gemini / Groq)..."
    ):
      ai_relevancy = analyze_relevancy_and_ugc_with_ai(
          qa_results.get("html_content", ""),
          target_niche,
          business_topic,
          qa_results,
      )

      # Reconcile DOM UGC check with AI verification
      if qa_results["is_ugc"] and ai_relevancy.get("niche_pass") != "Error":
        if not ai_relevancy.get("is_actual_ugc", True):
          # AI confirmed the backlink is in the editorial article, overriding DOM false positive
          qa_results["is_ugc"] = False
          qa_results["ugc_ai_verified"] = True
          qa_results["ugc_reason"] = (
              "DOM comment markup detected on page, but AI verified backlink is"
              f" placed in main editorial content ({ai_relevancy.get('ugc_audit_reason', '')})."
          )
        else:
          # AI confirmed the backlink is genuinely inside a comment/UGC section
          qa_results["ugc_ai_verified"] = True
          qa_results["ugc_reason"] = (
              f"{qa_results['ugc_reason']} — AI Confirmed:"
              f" {ai_relevancy.get('ugc_audit_reason', '')}"
          )

    st.markdown("---")
    st.subheader("📊 Full System QA Verification Report")

    if qa_results["error"]:
      st.error(f"System Blocked: {qa_results['error']}")

    # =========================================================================
    # SECTION 1: Audit Input Specifications & High-Level Summary
    # =========================================================================
    with st.container(border=True):
      st.markdown("### 1️⃣ Audit Input Specifications & High-Level Summary")

      sm1, sm2, sm3, sm4 = st.columns(4)
      with sm1:
        dupe_val = (
            "PASS (Unique)"
            if clickup_check["is_passed"]
            else "FAIL (Validation Error)"
        )
        st.metric(label="ClickUp Uniqueness", value=dupe_val)
      with sm2:
        st.metric(label="Domain Rating", value=f"DR {ahrefs_results['dr']}")
      with sm3:
        status = "Indexable" if qa_results["is_indexable"] else "NoIndex ❌"
        st.metric(label="Crawler Index Status", value=status)
      with sm4:
        brand_status = "Found" if qa_results["brand_mentioned"] else "Missing"
        st.metric(label="Brand Placement", value=brand_status)

      st.markdown("#### Input Target Context")
      spec_col1, spec_col2 = st.columns(2)
      with spec_col1:
        st.markdown(
            f"**Auto-Detected Client Folder:** `{detected_client}` (Folder ID:"
            f" `{detected_folder_id}`)"
        )
        st.markdown(f"**Live Page URL:** [{page_url}]({page_url})")
        st.markdown(f"**Target Landing URL:** [{target_url}]({target_url})")
      with spec_col2:
        st.markdown(f"**Customer Brand Name:** `{brand_name or 'N/A'}`")
        st.markdown(f"**Expected Anchor Text:** `{anchor_text or 'N/A'}`")
        st.markdown(
            f"**Target Niche / Topic:** `{target_niche or 'N/A'}` /"
            f" `{business_topic or 'N/A'}`"
        )

    # =========================================================================
    # SECTION 2: ClickUp Database Uniqueness Audit
    # =========================================================================
    with st.container(border=True):
      st.markdown("### 2️⃣ ClickUp Database Uniqueness Audit")

      if clickup_check["is_passed"]:
        st.success(
            "✅ **Verified Unique Placement!**  \n"
            f"- **Client Folder:** `{detected_client}` (Folder ID:"
            f" `{detected_folder_id}`)  \n"
            "- **Published URL:** Appears **exactly once** in this folder.  \n"
            f"- **Domain:** `{clickup_check['target_domain']}` is unique for"
            " this client folder."
        )
        matched_task = clickup_check["client_url_matches"][0]
        st.markdown(
            f"- **Active Task:** [{matched_task['task_name']}]({matched_task['task_url']})"
            f" | **Folder:** `{matched_task['client_folder']}` (`{matched_task['folder_id']}`)"
        )
      else:
        if clickup_check["url_count"] == 0:
          st.warning(
              "⚠️ **Missing ClickUp Record:** This URL was not found in the"
              " ClickUp `Published URL` field (0 occurrences). Please ensure it"
              " is encoded in ClickUp before running QA."
          )
        elif clickup_check["url_count"] > 1:
          st.error(
              "🚨 **Duplicate Published URL in Client Folder!**  \n"
              f"Expected 1 record in folder **{detected_client}** (ID:"
              f" `{detected_folder_id}`), but found"
              f" **{clickup_check['url_count']}** entries:"
          )
          for match in clickup_check["client_url_matches"]:
            st.markdown(
                f"- **Task:** [{match['task_name']}]({match['task_url']}) |"
                f" **Folder:** `{match['client_folder']}` (`{match['folder_id']}`)"
            )

        if clickup_check["domain_count"] > 1:
          st.error(
              "⚠️ **Client Domain Conflict:** Root domain"
              f" `{clickup_check['target_domain']}` appears"
              f" **{clickup_check['domain_count']}** times in folder"
              f" **{detected_client}**:"
          )
          for match in clickup_check["same_client_domain_matches"]:
            st.markdown(
                f"- **Task:** [{match['task_name']}]({match['task_url']}) |"
                f" **Live URL:** `{match['raw_url']}`"
            )

    # =========================================================================
    # SECTION 3: Technical Placement & Compliance Audit
    # =========================================================================
    with st.container(border=True):
      st.markdown("### 3️⃣ Technical Placement & Compliance Audit")

      tcol1, tcol2 = st.columns(2)
      with tcol1:
        st.markdown("**Page Level Integrity**")
        if qa_results["is_redirecting"]:
          st.warning(
              "⚠️ **Redirect Alert:** Destination resolved at:"
              f" `{qa_results['final_destination_url']}`"
          )
        else:
          st.success("✅ **Redirect Check:** Clean direct destination.")

        if qa_results["is_ugc"]:
          st.error(f"❌ **UGC Risk Confirmed:** {qa_results['ugc_reason']}")
        elif qa_results.get("ugc_ai_verified"):
          st.success(
              "✅ **Layout Check (AI Double-Checked):**"
              f" {qa_results['ugc_reason']}"
          )
        else:
          st.success("✅ **Layout Check:** Editorial article layout verified.")

        if qa_results["is_indexable"]:
          st.success("✅ **Meta Robots:** Page is indexable by crawlers.")
        else:
          st.error("❌ **Meta Robots:** `noindex` tag detected!")

      with tcol2:
        st.markdown("**Backlink & Node Verification**")
        if qa_results["link_found"]:
          st.success("✅ **Anchor Node Found:** Target backlink present in DOM.")
          if qa_results["anchor_matches"]:
            st.success(f"✅ **Anchor String:** Matches '{anchor_text}'")
          else:
            st.warning("⚠️ **Anchor String Discrepancy:** Anchor text mismatch.")

          if qa_results["is_follow"]:
            st.success("✅ **Attribution:** DoFollow link verified.")
          else:
            st.error(
                "❌ **Link Restricted:** Rel attributes:"
                f" `{qa_results['rel_tags']}`"
            )
        else:
          st.error(
              "❌ **Backlink Missing:** Target destination string was not"
              " found."
          )

      if ahrefs_results["traffic_history"]:
        st.markdown("#### 📈 Sitewide Authority Organic Trend (Ahrefs)")
        dates = [i.get("date") for i in ahrefs_results["traffic_history"]]
        traffic = [
            i.get("org_traffic", 0) for i in ahrefs_results["traffic_history"]
        ]
        st.line_chart(data=dict(zip(dates, traffic)))

    # =========================================================================
    # SECTION 4: Semantic AI Relevancy Audit
    # =========================================================================
    with st.container(border=True):
      st.markdown("### 4️⃣ Semantic AI Relevancy Audit (AI Evaluation)")

      r_col1, r_col2 = st.columns(2)
      with r_col1:
        if ai_relevancy.get("niche_pass") == "PASS":
          st.success(f"✅ **Niche Relevancy:** PASS ({target_niche})")
        else:
          st.error(f"❌ **Niche Relevancy:** FAIL ({target_niche})")

      with r_col2:
        if ai_relevancy.get("topic_pass") == "PASS":
          st.success(f"✅ **Topic Alignment:** PASS ({business_topic})")
        else:
          st.error(f"❌ **Topic Alignment:** FAIL ({business_topic})")

      st.info(f"🤖 **AI Auditor Reasoning Log:**\n\n{ai_relevancy['reason']}")

    # --- DOWNLOAD REPORT BUTTON ---
    st.markdown("---")
    st.subheader("📥 Export Complete Audit Report")

    pdf_buffer = generate_pdf_report(
        page_url=page_url,
        target_url=target_url,
        brand_name=brand_name,
        anchor_text=anchor_text,
        client_name=detected_client,
        folder_id=detected_folder_id,
        clickup_check=clickup_check,
        qa_results=qa_results,
        ahrefs_results=ahrefs_results,
        ai_relevancy=ai_relevancy,
    )

    st.download_button(
        label="📄 Download Full QA Audit Report (PDF)",
        data=pdf_buffer,
        file_name=f"Backlink_QA_Report_{normalize_domain(page_url)}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
