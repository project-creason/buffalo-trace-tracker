# 🥃 Buffalo Trace Bourbon Availability Tracker

Automatically checks [Buffalo Trace Distillery Product Availability](https://www.buffalotracedistillery.com/visit-us/product-availability/) during morning release hours via **GitHub Actions** and sends personalized **Email** and/or **ntfy.sh mobile push** alerts to each subscriber based on their individual bottle wishlist.

---

## How It Works

1. **Zero external dependencies**: [`check_bourbon.py`](./check_bourbon.py) uses only Python's standard library.
2. **Dual-layer parser**: Extracts live `in_stock` status from Buffalo Trace's embedded Next.js RSC data payload with an automatic HTML/SVG badge fallback.
3. **Personalized Wishlists**: Each person in [`subscribers.json`](./subscribers.json) chooses exactly which bottles they want to watch. If only *Blanton's* is in stock, only subscribers who asked for *Blanton's* receive an alert—others are not spammed.
4. **State-Based Deduplication**: Tracks state in [`last_state.json`](./last_state.json) so each subscriber only receives one confirmed alert per day, plus instant updates if a new bottle is added mid-morning.
5. **Private Delivery**: All emails are sent individually to each recipient (no shared headers, no accidental "reply-all", completely private).

---

## Managing Subscribers & Bottle Wishlists

Subscriber preferences are managed in [`subscribers.json`](./subscribers.json). You can edit this file directly in GitHub or push changes from your local repository.

### Example `subscribers.json`
```json
[
  {
    "name": "David Creason",
    "email": "davidcreason@gmail.com",
    "ntfy_topic": "bt-bourbon-creason-8492",
    "bottles": [
      "E.H. Taylor",
      "Blanton's",
      "Eagle Rare",
      "Weller Antique 107"
    ],
    "enabled": true
  },
  {
    "name": "Alice (Only Blanton's & Eagle Rare)",
    "email": "alice@example.com",
    "ntfy_topic": "",
    "bottles": [
      "Blanton's",
      "Eagle Rare"
    ],
    "enabled": true
  },
  {
    "name": "Bob (Any Rotating Offering)",
    "email": "bob@example.com",
    "bottles": [
      "rotating"
    ],
    "enabled": true
  }
]
```

### Supported Bottle Wishlist Keywords
You can match specific bottles or broad categories (case-insensitive):

| Wishlist Keyword | What It Matches |
| :--- | :--- |
| `"Blanton's"` or `"Blanton"` | *Blanton's Single Barrel*, *Blanton's Gold*, etc. |
| `"Eagle Rare"` | *Eagle Rare 10-Year*, *Eagle Rare 17*, etc. |
| `"E.H. Taylor"` or `"Taylor"` | *E.H. Taylor Small Batch*, *Single Barrel*, *Barrel Proof*, etc. |
| `"Weller Antique 107"` | Specifically *Weller Antique 107* (excludes Special Reserve) |
| `"Weller Special Reserve"` | Specifically *Weller Special Reserve* |
| `"Weller"` | All Weller offerings (*Antique 107*, *Special Reserve*, *12-Year*, *Full Proof*) |
| `"Stagg"` | *Stagg* / *George T. Stagg* |
| `"rotating"` or `"allocated"` | Any bottle currently in the **Rotating Offerings** section |
| `"all"` or `"*"` | Any available bottle in stock today |

---

## Required GitHub Secrets / Variables

In GitHub (`Settings → Secrets and variables → Actions`), ensure you have:

| Name | Type | Value |
| :--- | :--- | :--- |
| `GMAIL_USER` | Secret/Variable | Your sending Gmail address (e.g. `davidcreason@gmail.com`) |
| `GMAIL_APP_PASSWORD` | Secret | Your 16-character [Google App Password](https://myaccount.google.com/apppasswords) |

*(Optional fallback: If `subscribers.json` is ever empty, the script automatically falls back to `NOTIFY_EMAIL`, `NOTIFY_SMS_GATEWAY`, and `NTFY_TOPIC` environment variables).*

---

## Scheduling via Webhook (No Cron Delays)

Because GitHub Actions shared queues can delay native cron jobs during peak morning hours, the workflow accepts an external webhook trigger:

* **Endpoint**: `POST https://api.github.com/repos/project-creason/buffalo-trace-tracker/dispatches`
* **Headers**:
  * `Accept: application/vnd.github+json`
  * `Authorization: Bearer <GITHUB_PERSONAL_ACCESS_TOKEN>` *(Fine-grained token with `Contents: Read and write` permission)*
  * `User-Agent: CronJobOrg`
* **Body**: `{"event_type": "check_bourbon"}`
* **Schedule**: Every 15 minutes between 7:00 AM and 11:30 AM Eastern Time (via cron-job.org).
