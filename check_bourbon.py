#!/usr/bin/env python3
"""
Buffalo Trace Distillery Product Availability Checker & Notifier.

Fetches https://www.buffalotracedistillery.com/visit-us/product-availability/,
extracts the in-stock status of all bourbons, checks each subscriber's chosen
bottle wishlist from subscribers.json (e.g. Blanton's, E.H. Taylor, Eagle Rare,
Weller Antique 107), and sends personalized alerts via:
  - Email (Gmail SMTP)
  - Instant mobile push (ntfy.sh)
  - Carrier Email-to-SMS gateway

Supports smart state tracking via last_state.json to prevent duplicate alerts
while instantly catching morning drops and mid-day additions.
"""

import argparse
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import html as html_lib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request

URL = "https://www.buffalotracedistillery.com/visit-us/product-availability/"

try:
    from zoneinfo import ZoneInfo
    EASTERN_TZ = ZoneInfo("America/New_York")
except Exception:
    # Fallback timezone for Windows machines without tzdata package
    # Eastern Standard Time is UTC-5; Daylight Saving is UTC-4
    EASTERN_TZ = timezone(timedelta(hours=-5), name="EST")

DEFAULT_TARGETS = [
    "E.H. Taylor",
    "Blanton",
    "Eagle Rare",
    "Weller",
]


def fetch_page(url: str = URL) -> str:
    """Fetch the Buffalo Trace product availability page HTML."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_availability(page_html: str) -> list[dict]:
    """
    Parse bourbon availability from the page.
    Uses two methods for resilience:
      1. Primary: Next.js RSC embedded JSON payload ("in_stock":true|false,"name":"...")
      2. Fallback: Rendered HTML <div class="product"> cards and SVG badge fill colors.
    Returns a list of dicts: [{"name": str, "in_stock": bool, "category": str}]
    """
    products = []
    seen = set()

    # Method 1: Parse Next.js RSC JSON sections (Daily Offerings, Rotating Offerings, etc.)
    section_pattern = re.compile(
        r'\\"title\\":\\"(Daily Offerings|Rotating Offerings|Additional Offerings)\\".*?'
        r'\\"products\\":\[(.*?)\](?:,\\"cta\\"|})',
        re.DOTALL,
    )
    prod_pattern = re.compile(
        r'\\"in_stock\\":(true|false),\\"name\\":\\"([^\\"]+)\\"'
    )

    for sec_match in section_pattern.finditer(page_html):
        category = sec_match.group(1)
        products_blob = sec_match.group(2)
        for prod_match in prod_pattern.finditer(products_blob):
            in_stock = prod_match.group(1) == "true"
            name = html_lib.unescape(prod_match.group(2)).strip()
            if name not in seen:
                seen.add(name)
                products.append(
                    {
                        "name": name,
                        "in_stock": in_stock,
                        "category": category,
                    }
                )

    # If section wrapper changed but in_stock/name pairs still exist in RSC payload
    if not products:
        for prod_match in prod_pattern.finditer(page_html):
            in_stock = prod_match.group(1) == "true"
            name = html_lib.unescape(prod_match.group(2)).strip()
            if name not in seen:
                seen.add(name)
                products.append(
                    {
                        "name": name,
                        "in_stock": in_stock,
                        "category": "Gift Shop Offerings",
                    }
                )

    # Method 2: Fallback to rendered HTML <div class="product"> cards
    if not products:
        card_pattern = re.compile(
            r'<div class="product">(.*?)</a></div>', re.DOTALL
        )
        for card_match in card_pattern.finditer(page_html):
            card_html = card_match.group(1)
            name_match = re.search(r"<h4>(.*?)</h4>", card_html, re.DOTALL)
            if not name_match:
                continue
            name = html_lib.unescape(name_match.group(1)).strip()
            # Available badge uses rect fill="#AB431D" and checkmark path
            in_stock = 'fill="#AB431D"' in card_html
            if name not in seen:
                seen.add(name)
                products.append(
                    {
                        "name": name,
                        "in_stock": in_stock,
                        "category": "Gift Shop Offerings",
                    }
                )

    return products


def normalize_str(s: str) -> str:
    """Normalize string by standardizing quotes, apostrophes, and whitespace."""
    s = s.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def matches_target(product_name: str, targets: list[str], product_category: str = "") -> bool:
    """Return True if product_name matches any target keyword (case-insensitive)."""
    norm_prod = normalize_str(product_name)
    norm_cat = normalize_str(product_category)
    for target in targets:
        norm_t = normalize_str(target)
        if not norm_t:
            continue
        if norm_t in ("*", "all", "any"):
            return True
        if norm_t in ("rotating", "allocated") and "rotating" in norm_cat:
            return True
        if norm_t in norm_prod:
            return True
    return False


def load_subscribers(file_path: str = "subscribers.json") -> list[dict]:
    """
    Load subscriber profiles from subscribers.json.
    Falls back to legacy environment variables if the file is absent or empty.
    """
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                enabled = [s for s in data if isinstance(s, dict) and s.get("enabled", True)]
                if enabled:
                    return enabled
        except Exception as exc:
            print(f"Warning: could not read {file_path}: {exc}", file=sys.stderr)

    # Fallback to legacy environment variables
    email_to_raw = os.environ.get("NOTIFY_EMAIL", "").strip()
    sms_to_raw = os.environ.get("NOTIFY_SMS_GATEWAY", "").strip()
    ntfy_topic_raw = os.environ.get("NTFY_TOPIC", "").strip()
    target_raw = os.environ.get("TARGET_BOURBONS", "").strip()

    targets = (
        [t.strip() for t in target_raw.split(",") if t.strip()]
        if target_raw
        else DEFAULT_TARGETS
    )

    fallback_subs = []
    if email_to_raw:
        for em in [e.strip() for e in email_to_raw.split(",") if e.strip()]:
            fallback_subs.append(
                {
                    "name": em.split("@")[0],
                    "email": em,
                    "bottles": targets,
                    "enabled": True,
                }
            )

    if sms_to_raw:
        for sms in [s.strip() for s in sms_to_raw.split(",") if s.strip()]:
            fallback_subs.append(
                {
                    "name": "SMS Subscriber",
                    "sms_gateway": sms,
                    "bottles": targets,
                    "enabled": True,
                }
            )

    if ntfy_topic_raw:
        for topic in [t.strip() for t in ntfy_topic_raw.split(",") if t.strip()]:
            fallback_subs.append(
                {
                    "name": f"ntfy: {topic}",
                    "ntfy_topic": topic,
                    "bottles": targets,
                    "enabled": True,
                }
            )

    return fallback_subs


def load_state(state_file: str) -> dict:
    """Load previous alert state from JSON file if it exists."""
    if not state_file or not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Warning: could not read state file {state_file}: {exc}")
        return {}


def save_state(state_file: str, state: dict) -> None:
    """Save current alert state to JSON file."""
    if not state_file:
        return
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def is_within_schedule_window(now_et: datetime) -> tuple[bool, str]:
    """
    Check if current Eastern Time falls within the configured polling window:
      - All days (Mon - Sun): 6:30 AM - 11:30 AM ET (with buffer up to 11:40 AM)
    """
    minutes_since_midnight = now_et.hour * 60 + now_et.minute
    start_minutes = 6 * 60 + 30  # 6:30 AM ET
    end_minutes = 11 * 60 + 40    # 11:40 AM ET
    window_desc = "All days 6:30 AM - 11:30 AM ET"

    if start_minutes <= minutes_since_midnight <= end_minutes:
        return True, f"Inside polling window ({window_desc})"
    return False, f"Outside polling window ({window_desc})"


def should_alert_subscriber(
    sub_id: str,
    matched_names: list[str],
    subscriber_state: dict,
    now_et: datetime,
    force_notify: bool,
) -> tuple[bool, str]:
    """
    Evaluate whether a specific subscriber should receive an alert in this run:
      1. Always alert on --force-notify.
      2. If no target bottles are in stock, do not alert.
      3. If already alerted today: only alert if NEW matching bottles became available.
      4. If not alerted today:
         - If matched bottles differ from previous alert, alert immediately.
         - If matched bottles match previous alert, wait until >= 8:30 AM ET for daily confirmation.
    """
    if force_notify:
        return True, "Force notify enabled"

    if not matched_names:
        return False, "No requested bottles are currently in stock"

    today_str = now_et.strftime("%Y-%m-%d")
    sub_hist = subscriber_state.get(sub_id, {})
    last_date = sub_hist.get("last_alert_date", "")
    last_bottles = sub_hist.get("alerted_bottles", [])

    if last_date == today_str:
        new_bottles = [b for b in matched_names if b not in last_bottles]
        if new_bottles:
            return True, f"New bottle(s) added since last alert today: {', '.join(new_bottles)}"
        return False, f"Already alerted today for {', '.join(last_bottles)}"

    # New day
    if matched_names != last_bottles:
        return True, f"Lineup changed from previous alert ({last_bottles or 'none'} -> {matched_names})"

    # Lineup matches previous alert; confirm once after 8:30 AM ET
    minutes_since_midnight = now_et.hour * 60 + now_et.minute
    if minutes_since_midnight >= (8 * 60 + 30):
        return True, f"Confirmed post-8:30 AM ET daily alert for {today_str}"

    return (
        False,
        f"Pre-8:30 AM ET ({now_et.strftime('%I:%M %p ET')}): lineup matches previous alert; waiting for update or 8:30 AM confirmation.",
    )


def build_subscriber_email(
    sender: str,
    recipient: str,
    recipient_name: str,
    matched_available: list[dict],
    all_products: list[dict],
    now_et: datetime,
) -> MIMEMultipart:
    """Build a personalized HTML + plain-text email for a subscriber."""
    date_str = now_et.strftime("%b %d, %Y")
    names_short = ", ".join(p["name"] for p in matched_available)
    subject = f"🥃 Buffalo Trace Alert ({date_str}): {names_short}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"

    # Plain text version
    lines = [
        f"Buffalo Trace Distillery Availability Alert ({date_str})",
        "=" * 55,
        "",
        greeting,
        "",
        "The following bourbon(s) you are watching are AVAILABLE today at the Gift Shop:",
    ]
    for p in matched_available:
        lines.append(f"  [AVAILABLE] {p['name']} ({p['category']})")

    lines.append("")
    lines.append("ALL GIFT SHOP OFFERINGS TODAY:")
    for p in all_products:
        status = "AVAILABLE" if p["in_stock"] else "OUT OF STOCK"
        lines.append(f"  - {p['name']}: {status} [{p['category']}]")

    lines.extend([
        "",
        f"Distillery Availability: {URL}",
        "Live Tracker Dashboard: https://www.davidcreason.com/projects/buffalo-trace-tracker/",
    ])
    text_body = "\n".join(lines)

    # HTML version
    target_rows = "".join(
        f"<li style='margin:6px 0;font-size:16px;'>"
        f"✅ <strong>{html_lib.escape(p['name'])}</strong> "
        f"<span style='color:#666;font-size:13px;'>({html_lib.escape(p['category'])})</span>"
        f"</li>"
        for p in matched_available
    )

    all_rows = ""
    for p in all_products:
        badge = (
            "<span style='background:#AB431D;color:#FAF7ED;padding:2px 8px;"
            "border-radius:10px;font-size:12px;font-weight:bold;'>AVAILABLE</span>"
            if p["in_stock"]
            else "<span style='background:#D4CCB6;color:#183B35;padding:2px 8px;"
            "border-radius:10px;font-size:12px;'>OUT OF STOCK</span>"
        )
        all_rows += (
            f"<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;'>{html_lib.escape(p['name'])}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;color:#666;'>{html_lib.escape(p['category'])}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;'>{badge}</td>"
            f"</tr>"
        )

    html_body = f"""\
    <html>
      <body style="font-family:Arial,sans-serif;color:#222;max-width:600px;margin:0 auto;padding:12px;">
        <h2 style="color:#AB431D;margin-bottom:4px;">🥃 Buffalo Trace Bourbon Alert</h2>
        <p style="color:#555;margin-top:0;">Gift Shop Availability for <strong>{date_str}</strong></p>

        <p>{html_lib.escape(greeting)}</p>

        <div style="background:#FAF7ED;border-left:4px solid #AB431D;padding:12px 16px;margin:16px 0;">
          <h3 style="margin-top:0;color:#183B35;">Your Target Bottles Available Today:</h3>
          <ul style="padding-left:20px;margin-bottom:0;">
            {target_rows}
          </ul>
        </div>

        <h3 style="color:#183B35;margin-top:24px;">Full Distillery Lineup</h3>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead>
            <tr style="background:#f4f4f4;text-align:left;">
              <th style="padding:8px;">Bottle</th>
              <th style="padding:8px;">Category</th>
              <th style="padding:8px;">Status</th>
            </tr>
          </thead>
          <tbody>
            {all_rows}
          </tbody>
        </table>

        <p style="margin-top:20px;font-size:13px;">
          <a href="{URL}" style="color:#AB431D;font-weight:bold;">View Buffalo Trace Gift Shop &rarr;</a>
          &nbsp;|&nbsp;
          <a href="https://www.davidcreason.com/projects/buffalo-trace-tracker/" style="color:#183B35;font-weight:bold;">Live Tracker Dashboard &rarr;</a>
        </p>
      </body>
    </html>
    """

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def build_subscriber_sms(
    sender: str,
    recipient: str,
    matched_available: list[dict],
) -> EmailMessage:
    """Build a compact plain-text message for Email-to-SMS gateways."""
    names = ", ".join(p["name"] for p in matched_available)
    body = f"Buffalo Trace Today: {names} AVAILABLE at Gift Shop!"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = "BT Alert"
    msg.set_content(body)
    return msg


def send_ntfy_push(
    topic: str,
    matched_available: list[dict],
    all_products: list[dict],
    now_et: datetime,
) -> None:
    """Send an instant mobile push notification via ntfy.sh."""
    date_str = now_et.strftime("%b %d")
    target_names = ", ".join(p["name"] for p in matched_available)

    rotating_lines = []
    for p in all_products:
        if p["category"] == "Rotating Offerings":
            icon = "✅" if p["in_stock"] else "❌"
            rotating_lines.append(f"{icon} {p['name']}")

    body_lines = [f"Available today: {target_names}"]
    if rotating_lines:
        body_lines.append("")
        body_lines.append("Rotating Lineup:")
        body_lines.extend(rotating_lines)

    payload = "\n".join(body_lines).encode("utf-8")
    clean_topic = topic.strip().lstrip("/")
    ntfy_url = (
        clean_topic
        if clean_topic.startswith("http")
        else f"https://ntfy.sh/{clean_topic}"
    )

    req = urllib.request.Request(
        ntfy_url,
        data=payload,
        method="POST",
        headers={
            "Title": f"Buffalo Trace Alert ({date_str})",
            "Priority": "high",
            "Tags": "tumbler_glass,white_check_mark",
            "Click": URL,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    print(f"Sent ntfy.sh mobile push notification to topic: {clean_topic}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Buffalo Trace product availability and send subscriber-specific alerts."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print availability without sending notifications.",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Send notification even if lineup hasn't changed or only standard bottles are available.",
    )
    parser.add_argument(
        "--subscribers-file",
        default="subscribers.json",
        help="Path to JSON file containing subscriber wishlist profiles.",
    )
    parser.add_argument(
        "--state-file",
        default="last_state.json",
        help="Path to JSON file tracking last alerted lineup to prevent duplicate notifications.",
    )
    args = parser.parse_args()

    now_et = datetime.now(EASTERN_TZ)
    print(f"Current Eastern Time: {now_et.strftime('%A, %Y-%m-%d %I:%M:%S %p %Z')}")

    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state = load_state(args.state_file)
    today_str = now_et.strftime("%Y-%m-%d")
    already_alerted_today = state.get("last_alert_date") == today_str

    if not is_manual_run and not args.force_notify:
        in_window, window_reason = is_within_schedule_window(now_et)
        print(f"Schedule check: {window_reason}")
        if not in_window and already_alerted_today:
            print("Skipping check: outside active window and today's alert was already sent.")
            return
        elif not in_window and not already_alerted_today and now_et.hour >= 8:
            print("Running after window, but no alert has been sent yet today — proceeding with check!")
        elif not in_window:
            print("Skipping pre-6:30 AM ET check.")
            return

    print(f"Fetching {URL} ...")
    page_html = fetch_page(URL)
    products = parse_availability(page_html)

    if not products:
        print("ERROR: Could not parse any products from the page!", file=sys.stderr)
        sys.exit(2)

    subscribers = load_subscribers(args.subscribers_file)
    print(f"Loaded {len(subscribers)} active subscriber profile(s) from {args.subscribers_file}.")

    print("\nToday's Distillery Availability:")
    print("-" * 60)
    for p in products:
        status_icon = "[AVAILABLE]   " if p["in_stock"] else "[OUT OF STOCK]"
        print(f"  {status_icon} {p['name']} ({p['category']})")
    print("-" * 60)

    # Evaluate each subscriber
    subscriber_state = state.get("subscriber_state", {})
    subscribers_to_alert = []

    for sub in subscribers:
        sub_name = sub.get("name", "Subscriber")
        sub_id = sub.get("email") or sub.get("sms_gateway") or sub.get("ntfy_topic") or sub_name
        wishlist = sub.get("bottles") or DEFAULT_TARGETS

        matched = [
            p
            for p in products
            if p["in_stock"] and matches_target(p["name"], wishlist, p.get("category", ""))
        ]

        if args.force_notify and not matched:
            matched = [p for p in products if p["in_stock"]]

        matched_names = [p["name"] for p in matched]

        should_alert, reason = should_alert_subscriber(
            sub_id=sub_id,
            matched_names=matched_names,
            subscriber_state=subscriber_state,
            now_et=now_et,
            force_notify=args.force_notify,
        )

        status_prefix = "-> WILL ALERT" if should_alert else "-- SKIP"
        print(f"\n{status_prefix}: {sub_name} (watching: {', '.join(wishlist)})")
        print(f"   In-stock matches: {', '.join(matched_names) or 'None'}")
        print(f"   Reason: {reason}")

        if should_alert:
            subscribers_to_alert.append((sub, sub_id, matched, matched_names))

    # Backward compatibility: update root fields in last_state.json for web dashboard
    all_in_stock = [p["name"] for p in products if p["in_stock"]]
    all_target_matches = [
        p["name"]
        for p in products
        if p["in_stock"] and matches_target(p["name"], DEFAULT_TARGETS, p.get("category", ""))
    ]

    products_changed = state.get("products") != products
    if not subscribers_to_alert:
        print("\nNo subscribers require alerts in this run.")
        if not args.dry_run and products_changed:
            state["last_checked_et"] = now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z")
            state["all_available"] = all_in_stock
            state["available_targets"] = all_target_matches
            state["products"] = products
            save_state(args.state_file, state)
            print(f"Updated product snapshot in {args.state_file}")
        return

    if args.dry_run:
        print(f"\n--dry-run enabled; would alert {len(subscribers_to_alert)} subscriber(s).")
        return

    # Dispatch alerts
    smtp_user = os.environ.get("GMAIL_USER", "").strip()
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    smtp_server = None

    try:
        for sub, sub_id, matched, matched_names in subscribers_to_alert:
            # 1. Email notification
            email_addr = sub.get("email", "").strip()
            if email_addr:
                if not smtp_user or not smtp_pass:
                    print(f"WARNING: Cannot email {email_addr}; GMAIL_USER or GMAIL_APP_PASSWORD missing.", file=sys.stderr)
                else:
                    if smtp_server is None:
                        context = ssl.create_default_context()
                        smtp_server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context)
                        smtp_server.login(smtp_user, smtp_pass)

                    email_msg = build_subscriber_email(
                        sender=smtp_user,
                        recipient=email_addr,
                        recipient_name=sub.get("name", ""),
                        matched_available=matched,
                        all_products=products,
                        now_et=now_et,
                    )
                    smtp_server.sendmail(smtp_user, [email_addr], email_msg.as_string())
                    print(f"Sent email alert to: {sub.get('name') or email_addr} ({email_addr})")

            # 2. SMS Gateway notification
            sms_addr = sub.get("sms_gateway", "").strip()
            if sms_addr:
                if not smtp_user or not smtp_pass:
                    print(f"WARNING: Cannot SMS {sms_addr}; GMAIL_USER or GMAIL_APP_PASSWORD missing.", file=sys.stderr)
                else:
                    if smtp_server is None:
                        context = ssl.create_default_context()
                        smtp_server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context)
                        smtp_server.login(smtp_user, smtp_pass)

                    sms_msg = build_subscriber_sms(smtp_user, sms_addr, matched)
                    smtp_server.send_message(sms_msg)
                    print(f"Sent SMS gateway alert to: {sms_addr}")

            # 3. Mobile push notification (ntfy.sh)
            ntfy_topic = sub.get("ntfy_topic", "").strip()
            if ntfy_topic:
                send_ntfy_push(ntfy_topic, matched, products, now_et)

            # Record state for this subscriber
            subscriber_state[sub_id] = {
                "last_alert_date": today_str,
                "last_alert_timestamp_et": now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "alerted_bottles": matched_names,
            }
    finally:
        if smtp_server is not None:
            try:
                smtp_server.quit()
            except Exception:
                pass

    # Update overall state
    state["last_checked_et"] = now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    state["all_available"] = all_in_stock
    state["available_targets"] = all_target_matches
    state["products"] = products
    state["last_alert_date"] = today_str
    state["last_alert_timestamp_et"] = now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    state["subscriber_state"] = subscriber_state

    save_state(args.state_file, state)
    print(f"\nSuccessfully updated state file: {args.state_file}")


if __name__ == "__main__":
    main()
