#!/usr/bin/env python3
"""
Buffalo Trace Distillery Product Availability Checker & Notifier.

Fetches https://www.buffalotracedistillery.com/visit-us/product-availability/,
extracts the in-stock status of all bourbons (specifically watching allocated
bottles like Blanton's, E.H. Taylor, Eagle Rare, and Weller), and sends an
Email and/or SMS alert via Gmail SMTP + Carrier Email-to-SMS Gateway when
target bottles are available.
"""

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import html as html_lib
import os
import re
import smtplib
import ssl
import sys
import urllib.request

URL = "https://www.buffalotracedistillery.com/visit-us/product-availability/"

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


def build_email_message(
    sender: str,
    recipients: list[str],
    matched_available: list[dict],
    all_products: list[dict],
) -> MIMEMultipart:
    """Build a rich HTML + plain-text email summarizing today's bourbon availability."""
    date_str = datetime.now().strftime("%b %d, %Y")
    names_short = ", ".join(p["name"] for p in matched_available)
    subject = f"🥃 Buffalo Trace Alert ({date_str}): {names_short}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

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
    # Empty/short subject avoids taking up character budget on SMS gateways
    msg["Subject"] = "BT Alert"
    msg.set_content(body)
    return msg


def send_notifications(
    matched_available: list[dict],
    all_products: list[dict],
) -> None:
    """Send Email and/or SMS alerts using Gmail SMTP credentials from env vars."""
    smtp_user = os.environ.get("GMAIL_USER", "").strip()
    smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    email_to_raw = os.environ.get("NOTIFY_EMAIL", "").strip()
    sms_to_raw = os.environ.get("NOTIFY_SMS_GATEWAY", "").strip()

    if not smtp_user or not smtp_pass:
        print(
            "ERROR: GMAIL_USER and GMAIL_APP_PASSWORD environment variables are required to send alerts.",
            file=sys.stderr,
        )
        sys.exit(1)

    email_recipients = [e.strip() for e in email_to_raw.split(",") if e.strip()]
    sms_recipients = [s.strip() for s in sms_to_raw.split(",") if s.strip()]

    if not email_recipients and not sms_recipients:
        print(
            "ERROR: Set at least one of NOTIFY_EMAIL or NOTIFY_SMS_GATEWAY.",
            file=sys.stderr,
        )
        sys.exit(1)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(smtp_user, smtp_pass)

        if email_recipients:
            email_msg = build_email_message(
                smtp_user, email_recipients, matched_available, all_products
            )
            server.sendmail(smtp_user, email_recipients, email_msg.as_string())
            print(f"Sent email alert to: {', '.join(email_recipients)}")

        if sms_recipients:
            sms_msg = build_sms_message(
                smtp_user, sms_recipients, matched_available
            )
            server.send_message(sms_msg)
            print(f"Sent SMS gateway alert to: {', '.join(sms_recipients)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Buffalo Trace product availability and send Email/SMS alerts."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print availability without sending Email/SMS notifications.",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Send notification even if only standard bottles are available (useful for testing).",
    )
    args = parser.parse_args()

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
        print("\nNo target allocated bottles are in stock today. No alert sent.")
        return

    print(
        "\nTarget bottles AVAILABLE today: "
        + ", ".join(p["name"] for p in matched_available)
    )

    if args.dry_run:
        print("\n--dry-run enabled; skipping Email/SMS send.")
        return

    send_notifications(matched_available, products)


if __name__ == "__main__":
    main()
