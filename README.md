# 🥃 Buffalo Trace Bourbon Availability Tracker

Automatically checks [Buffalo Trace Distillery Product Availability](https://www.buffalotracedistillery.com/visit-us/product-availability/) every morning via **GitHub Actions** and sends you an **Email** and/or **Text Message (SMS)** when allocated bottles (**Blanton's**, **E.H. Taylor**, **Eagle Rare**, **Weller**) are in stock.

---

## How It Works

1. **Zero external dependencies**: [`check_bourbon.py`](./check_bourbon.py) uses only Python's standard library, so GitHub Actions runs in ~2 seconds.
2. **Dual-layer parser**: Extracts live `in_stock` status from Buffalo Trace's embedded Next.js data payload (`Daily Offerings`, `Rotating Offerings`, `Additional Offerings`) with an automatic HTML/SVG badge fallback.
3. **Smart filtering**: Only sends an alert when at least one of your target allocated bourbons is marked **Available** (defaults to `E.H. Taylor, Blanton, Eagle Rare, Weller`).
4. **Email + Free SMS**: Sends a formatted HTML email to your inbox and a compact `<160 char` text message via your mobile carrier's free Email-to-SMS gateway.

---

## Quick Setup (3 Minutes)

### Step 1: Create a Free Gmail App Password
1. Go to your Google Account's **[App Passwords page](https://myaccount.google.com/apppasswords)** (requires 2-Step Verification enabled on your Google account).
2. Enter a name like `Buffalo Trace Tracker` and click **Create**.
3. Copy the 16-character password shown (e.g. `abcd efgh ijkl mnop`).

### Step 2: Find Your Carrier's Email-to-SMS Address
Replace `5551234567` with your 10-digit mobile number:

| Mobile Carrier | SMS Gateway Address |
| :--- | :--- |
| **Verizon** | `5551234567@vtext.com` |
| **AT&T** | `5551234567@txt.att.net` |
| **T-Mobile / Mint** | `5551234567@tmomail.net` |
| **Google Fi** | `5551234567@msg.fi.google.com` |
| **US Cellular** | `5551234567@email.uscc.net` |
| **Cricket** | `5551234567@sms.cricketwireless.net` |
| **Boost Mobile** | `5551234567@sms.myboostmobile.com` |

### Step 3: Push to GitHub & Add Secrets
1. Create a new repository on GitHub (can be **Private** or **Public**) and push this folder to it.
2. In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret Name | Value | Required? |
| :--- | :--- | :--- |
| `GMAIL_USER` | Your Gmail address (e.g. `you@gmail.com`) | **Yes** |
| `GMAIL_APP_PASSWORD` | Your 16-character Gmail App Password | **Yes** |
| `NOTIFY_EMAIL` | Email address(es) to receive the full table (comma-separated) | At least one of Email or SMS |
| `NOTIFY_SMS_GATEWAY` | Carrier SMS gateway address(es) (e.g. `5551234567@vtext.com`) | At least one of Email or SMS |

*(Optional)* Under the **Variables** tab (`Settings → Secrets and variables → Actions → Variables`), you can create a variable named `TARGET_BOURBONS` to customize which bottles trigger an alert:
- Default if omitted: `E.H. Taylor, Blanton, Eagle Rare, Weller`
- Example custom value: `E.H. Taylor, Blanton, Eagle Rare, Weller Antique` (if you want to exclude Weller Special Reserve, or include only specific bottles).

### Step 4: Test It Immediately
1. In your GitHub repository, click the **Actions** tab.
2. Select **Buffalo Trace Daily Bourbon Alert** on the left.
3. Click **Run workflow** → check **Send test alert** (or leave unchecked since allocated bottles are in stock right now!) → click **Run workflow**.
