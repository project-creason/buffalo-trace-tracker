#!/usr/bin/env python3
"""
Buffalo Trace Distillery Product Availability Checker & Notifier.

Fetches https://www.buffalotracedistillery.com/visit-us/product-availability/,
extracts the in-stock status of all bourbons (specifically watching allocated
bottles like Blanton's, E.H. Taylor, Eagle Rare, and Weller), and sends:
  - Instant mobile push notifications via ntfy.sh (free iOS/Android app)
  - Email alerts via Gmail SMTP (with recipients hidden in BCC)
  - Carrier Email-to-SMS gateway alerts (for carriers that still support it)

Supports smart change-detection via a state file (last_state.json) so it can
poll every 10-15 minutes during morning release hours and alert you the exact
moment the lineup changes (or once the gift shop opens at 9:00 AM ET) without
sending duplicate alerts.
"""

import argparse
from datetime import datetime
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
from zoneinfo import ZoneInfo

URL = "https://www.buffalotracedistillery.com/visit-us/product-availability/"
EASTERN_TZ = ZoneInfo("America/New_York")

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


def get_target_keywords() -> list[str]:
    """Read target bourbon keywords from TARGET_BOURBONS env var or use defaults."""
    raw = os.environ.get("TARGET_BOURBONS", "").strip()
    if not raw:
        return DEFAULT_TARGETS
    return [item.strip() for item in raw.split(",") if item.strip()]


def matches_target(product_name: str, targets: list[str]) -> bool:
    """Return True if product_name matches any target keyword (case-insensitive)."""
    lower_name = product_name.lower()
    return any(target.lower() in lower_name for target in targets)


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


def should_send_alert(
    state: dict,
    current_targets: list[str],
    now_et: datetime,
    is_manual_run: bool,
    force_notify: bool,
) -> tuple[bool, str]:
    """
    Determine whether to send an alert right now:
      1. Always send on manual workflow_dispatch runs or --force-notify.
      2. Immediately send if the in-stock target bottle list changed since the last alert.
      3. Once it is >= 8:30 AM ET (all 7 days, since Sunday's page is also updated ahead
         of the 11:00 AM Gift Shop opening), send today's daily alert if we haven't sent
         one yet for today's date (even if today's bottles happen to match yesterday's).
      4. Otherwise skip (prevents duplicate alerts every 15 minutes).
    """
    if force_notify:
        return True, "Force notify enabled"
    if is_manual_run:
        return True, "Manual workflow run triggered"

    today_str = now_et.strftime("%Y-%m-%d")
    last_date = state.get("last_alert_date", "")
    last_targets = state.get("available_targets", [])

    # Did the available target bottles change compared to the last alert?
    if current_targets != last_targets:
        return True, f"Lineup changed from {last_targets or 'none'} to {current_targets}"

    # If we already alerted for this exact lineup today, don't spam
    if last_date == today_str:
        return (
            False,
            f"Already alerted today ({today_str}) for {', '.join(current_targets)}; no change.",
        )

    # It's a new day and the lineup is identical to yesterday's last alert.
    # Confirm at 8:30 AM ET (all 7 days, including Sunday) so morning alerts aren't held until 11 AM.
    minutes_since_midnight = now_et.hour * 60 + now_et.minute
    if minutes_since_midnight >= (8 * 60 + 30):
        return (
            True,
            f"First confirmed post-8:30AM ET daily alert for {today_str} (same lineup as yesterday)",
        )

    return (
        False,
        f"Pre-8:30AM ET check ({now_et.strftime('%I:%M %p ET')}): lineup unchanged from yesterday, waiting for update or 8:30 AM ET confirmation.",
    )


def build_email_message(
    sender: str,
    recipients: list[str],
    matched_available: list[dict],
    all_products: list[dict],
    now_et: datetime,
) -> MIMEMultipart:
    """Build a rich HTML + plain-text email summarizing today's bourbon availability."""
    date_str = now_et.strftime("%b %d, %Y")
    names_short = ", ".join(p["name"] for p in matched_available)
    subject = f"🥃 Buffalo Trace Alert ({date_str}): {names_short}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    # Set To: header to the sender only so all NOTIFY_EMAIL recipients are BCC'd
    # and cannot see each other's email addresses.
    msg["To"] = sender

    # Plain-text body
    lines = [
        f"Buffalo Trace Distillery Availability Alert ({date_str})",
        "=" * 55,
        "",
        "TARGET ALLOCATED BOTTLES IN STOCK TODAY:",
    ]
    for p in matched_available:
        lines.append(f"  [AVAILABLE] {p['name']} ({p['category']})")

    lines.append("")
    lines.append("ALL GIFT SHOP OFFERINGS TODAY:")
    for p in all_products:
        status = "AVAILABLE" if p["in_stock"] else "OUT OF STOCK"
        lines.append(f"  - {p['name']}: {status} [{p['category']}]")

    lines.extend(["", f"Check live page: {URL}"])
    text_body = "\n".join(lines)

    # HTML body
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
      <body style="font-family:Arial,sans-serif;color:#222;max-width:600px;margin:0 auto;">
        <h2 style="color:#AB431D;margin-bottom:4px;">🥃 Buffalo Trace Bourbon Alert</h2>
        <p style="color:#555;margin-top:0;">Gift Shop Availability for <strong>{date_str}</strong></p>

        <div style="background:#FAF7ED;border-left:4px solid #AB431D;padding:12px 16px;margin:16px 0;">
          <h3 style="margin-top:0;color:#183B35;">Target Bottles Available Today:</h3>
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
          <a href="{URL}" style="color:#AB431D;font-weight:bold;">View Live Availability Page &rarr;</a>
        </p>
      </body>
    </html>
    """

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def build_sms_message(
    sender: str,
    sms_recipients: list[str],
    matched_available: list[dict],
) -> EmailMessage:
    """
    Build a compact plain-text message for Carrier Email-to-SMS gateways
    (kept concise so carrier gateways deliver it cleanly as a single text).
    """
    names = ", ".join(p["name"] for p in matched_available)
    body = f"Buffalo Trace Today: {names} AVAILABLE at Gift Shop!"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(sms_recipients)
    msg["Subject"] = "BT Alert"
    msg.set_content(body)
    return msg


def send_ntfy_push(
    topics: list[str],
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

    for topic in topics:
        clean_topic = topic.strip().lstrip("/")
        if not clean_topic:
            continue
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


def send_notifications(
    matched_available: list[dict],
    all_products: list[dict],
    now_et: datetime,
) -> None:
    """Send ntfy.sh mobile push, Email, and/or SMS alerts based on configured env vars."""
    smtp_user = os.environ.get("GMAIL_USER", "").strip()
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    email_to_raw = os.environ.get("NOTIFY_EMAIL", "").strip()
    sms_to_raw = os.environ.get("NOTIFY_SMS_GATEWAY", "").strip()
    ntfy_topic_raw = os.environ.get("NTFY_TOPIC", "").strip()

    email_recipients = [e.strip() for e in email_to_raw.split(",") if e.strip()]
    sms_recipients = [s.strip() for s in sms_to_raw.split(",") if s.strip()]
    ntfy_topics = [t.strip() for t in ntfy_topic_raw.split(",") if t.strip()]

    sent_any = False

    # 1. Send ntfy.sh push notification if NTFY_TOPIC is configured
    if ntfy_topics:
        send_ntfy_push(ntfy_topics, matched_available, all_products, now_et)
        sent_any = True

    # 2. Send Email / SMS via Gmail SMTP if configured
    if email_recipients or sms_recipients:
        if not smtp_user or not smtp_pass:
            print(
                "WARNING: NOTIFY_EMAIL / NOTIFY_SMS_GATEWAY set, but GMAIL_USER or GMAIL_APP_PASSWORD is missing.",
                file=sys.stderr,
            )
            if not sent_any:
                sys.exit(1)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                server.login(smtp_user, smtp_pass)

                if email_recipients:
                    email_msg = build_email_message(
                        smtp_user,
                        email_recipients,
                        matched_available,
                        all_products,
                        now_et,
                    )
                    server.sendmail(smtp_user, email_recipients, email_msg.as_string())
                    print(
                        f"Sent BCC email alert to {len(email_recipients)} recipient(s)."
                    )
                    sent_any = True

                if sms_recipients:
                    sms_msg = build_sms_message(
                        smtp_user, sms_recipients, matched_available
                    )
                    server.send_message(sms_msg)
                    print(f"Sent SMS gateway alert to: {', '.join(sms_recipients)}")
                    sent_any = True

    if not sent_any:
        print(
            "ERROR: No notification destination configured. Set NTFY_TOPIC, NOTIFY_EMAIL, or NOTIFY_SMS_GATEWAY.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Buffalo Trace product availability and send mobile/email alerts."
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
        "--state-file",
        default="last_state.json",
        help="Path to JSON file tracking last alerted lineup to prevent duplicate notifications.",
    )
    args = parser.parse_args()

    now_et = datetime.now(EASTERN_TZ)
    print(f"Current Eastern Time: {now_et.strftime('%A, %Y-%m-%d %I:%M:%S %p %Z')}")

    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state = load_state(args.state_file)
    already_alerted_today = state.get("last_alert_date") == now_et.strftime("%Y-%m-%d")

    if not is_manual_run and not args.force_notify:
        in_window, window_reason = is_within_schedule_window(now_et)
        print(f"Schedule check: {window_reason}")
        if not in_window and already_alerted_today:
            print("Skipping check because current Eastern Time is outside the active window and today's alert was already sent.")
            return
        elif not in_window and not already_alerted_today and now_et.hour >= 8:
            print("GitHub cron ran after the target window, but no alert has been sent yet today — proceeding with check!")
        elif not in_window:
            print("Skipping pre-6:30 AM ET check.")
            return

    print(f"Fetching {URL} ...")

    page_html = fetch_page(URL)
    products = parse_availability(page_html)

    if not products:
        print("ERROR: Could not parse any products from the page!", file=sys.stderr)
        sys.exit(2)

    targets = get_target_keywords()
    print(f"Target keywords: {', '.join(targets)}\n")

    print("Today's Distillery Availability:")
    print("-" * 60)
    for p in products:
        status_icon = "[AVAILABLE]   " if p["in_stock"] else "[OUT OF STOCK]"
        print(f"  {status_icon} {p['name']} ({p['category']})")
    print("-" * 60)

    matched_available = [
        p
        for p in products
        if p["in_stock"] and matches_target(p["name"], targets)
    ]

    if args.force_notify and not matched_available:
        matched_available = [p for p in products if p["in_stock"]]

    if not matched_available:
        print("\nNo target allocated bottles are in stock right now. No alert sent.")
        return

    current_targets = [p["name"] for p in matched_available]
    print("\nTarget bottles AVAILABLE: " + ", ".join(current_targets))

    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state = load_state(args.state_file)

    should_send, reason = should_send_alert(
        state=state,
        current_targets=current_targets,
        now_et=now_et,
        is_manual_run=is_manual_run,
        force_notify=args.force_notify,
    )
    print(f"Alert decision: {should_send} ({reason})")

    if not should_send:
        if not args.dry_run and state.get("products") != products:
            state["last_checked_et"] = now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z")
            state["products"] = products
            save_state(args.state_file, state)
            print(f"Updated product snapshot in {args.state_file}")
        return

    if args.dry_run:
        print("\n--dry-run enabled; skipping notification send and state update.")
        return

    send_notifications(matched_available, products, now_et)

    new_state = {
        "last_checked_et": now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "last_alert_date": now_et.strftime("%Y-%m-%d"),
        "last_alert_timestamp_et": now_et.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "available_targets": current_targets,
        "all_available": [p["name"] for p in products if p["in_stock"]],
        "products": products,
    }
    save_state(args.state_file, new_state)
    print(f"Updated state file: {args.state_file}")


if __name__ == "__main__":
    main()
