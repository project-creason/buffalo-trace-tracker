# 🥃 Buffalo Trace Bourbon Availability Tracker

Automatically checks [Buffalo Trace Distillery Product Availability](https://www.buffalotracedistillery.com/visit-us/product-availability/) every morning via **GitHub Actions** and sends you an **instant mobile push notification (via ntfy.sh)** and/or **Email** when allocated bottles (**Blanton's**, **E.H. Taylor**, **Eagle Rare**, **Weller**) are in stock.

---

## How It Works

1. **Zero external dependencies**: [`check_bourbon.py`](./check_bourbon.py) uses only Python's standard library, so GitHub Actions runs in ~2 seconds.
2. **Dual-layer parser**: Extracts live `in_stock` status from Buffalo Trace's embedded Next.js data payload (`Daily Offerings`, `Rotating Offerings`, `Additional Offerings`) with an automatic HTML/SVG badge fallback.
3. **Smart filtering**: Only sends an alert when at least one of your target allocated bourbons is marked **Available** (defaults to `E.H. Taylor, Blanton, Eagle Rare, Weller`).
4. **Instant Mobile Push + Email**:
   - **ntfy.sh Mobile Push (Recommended for AT&T & all carriers)**: 100% free iOS/Android push notifications with no signup or API key required. Anyone subscribed to your topic name gets an instant high-priority phone notification that opens the Buffalo Trace availability page when tapped.
   - **Gmail HTML Email**: Sends a formatted availability table to one or more email addresses.

---

## Quick Setup

### Option A: Instant Phone Alerts via `ntfy.sh` (30 Seconds, No Account Needed)
1. Install the free **ntfy** app on your phone ([iOS App Store](https://apps.apple.com/us/app/ntfy/id1625396347) / [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Open the app, tap **`+`**, and subscribe to a unique topic name of your choice (for example: `bt-bourbon-creason-8492`).
   - *Tip: Friends or family who also want alerts can simply subscribe to the same topic name in their ntfy app!*
3. In your GitHub repo (`Settings → Secrets and variables → Actions`), add a Secret or Variable named **`NTFY_TOPIC`** with that exact topic name (`bt-bourbon-creason-8492`).

### Option B: Email Alerts via Gmail
1. Go to your Google Account's **[App Passwords page](https://myaccount.google.com/apppasswords)** (requires 2-Step Verification enabled).
2. Create an app password named `Buffalo Trace Tracker` and copy the 16-character code.
3. Add these Secrets or Variables in GitHub (`Settings → Secrets and variables → Actions`):

| Name | Value | Required? |
| :--- | :--- | :--- |
| `NTFY_TOPIC` | Your ntfy.sh topic name (e.g. `bt-bourbon-creason-8492`) | Optional (Recommended for phone alerts) |
| `GMAIL_USER` | Your Gmail address (e.g. `you@gmail.com`) | Required if using `NOTIFY_EMAIL` |
| `GMAIL_APP_PASSWORD` | Your 16-character Gmail App Password | Required if using `NOTIFY_EMAIL` |
| `NOTIFY_EMAIL` | Email address(es) to receive the full table (comma-separated) | Optional |
| `TARGET_BOURBONS` | Custom comma-separated keywords (defaults to `E.H. Taylor, Blanton, Eagle Rare, Weller`) | Optional |

*(Note: AT&T permanently shut down `@txt.att.net` on June 17, 2025. Use `NTFY_TOPIC` for reliable mobile push alerts on AT&T and other carriers.)*
