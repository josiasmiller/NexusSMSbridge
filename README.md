# NexusBridge
A complete SMS Bridge system that connects an Android phone to a web-based messaging UI via a WebSocket relay server.

## Get Started

👉 **Go to [https://sms-bridge-jmtdi.devpush.app/](https://sms-bridge-jmtdi.devpush.app/)**

The builder will walk you through three steps:

1. **Deploy your server** — click the "Deploy to Railway" button to spin up a free server in one click
2. **Enter your server URL** — paste the Railway URL you get after deploying
3. **Build your app** — the builder generates and downloads a zip containing:
   - `nexusbridge.apk` — ready-to-install Android app
   - `index.html` — web messaging UI, pre-configured for your server

No coding required.

---

## Self-Hosting the Builder

If you want to run the builder yourself instead of using the hosted version:

```bash
git clone https://github.com/JMTDI/sms-bridge
cd sms-bridge
pip install -r requirements.txt
python builder.py
```

Open **http://localhost:5000** in your browser.

## Architecture

```
Android App (phone) ──WebSocket──▶ server.py ──WebSocket──▶ index.html (browser)
                                       │
                                  REST API
                                  /new-session
                                  /session-status/{token}
```

## Components

### 1. `server.py` — Python Bridge Server

Runs on port 8000 (HTTP + WebSocket on the same port).

**Setup:**
```bash
pip install -r requirements.txt
python server.py
```

**Endpoints:**
- `GET /` → serves `index.html`
- `GET /new-session` → creates a new session, returns `{ sessionToken, pin, qrData }`
- `GET /session-status/{token}` → returns phone connection status
- `GET /health` → health check
- `wss://host/ws/{token}?role=phone|client` → WebSocket bridge

### 2. `index.html` — Web Client

Single-file, no build step required. Open directly in a browser or via `https://yourserver.com/`.

Features:
- Pairing screen with QR code + 6-digit PIN
- Full conversation list + thread view
- Real-time messaging via WebSocket
- Dark theme, fully responsive
- Session saved in `localStorage` for reconnection

### 3. Android App (`android/`)


Package: `com.nexusbridge.smsbridge`

**Features:**
- D-pad navigable numpad for PIN entry
- ZXing QR code scanner
- Foreground service maintaining persistent WebSocket connection
- Reads SMS/MMS via ContentProvider
- Sends SMS via SmsManager
- BroadcastReceiver for incoming SMS
- Exponential backoff reconnection

**Build:**
Open `android/` folder in Android Studio and run on device.

**Required Permissions:** READ_SMS, SEND_SMS, RECEIVE_SMS, READ_CONTACTS, INTERNET, FOREGROUND_SERVICE, POST_NOTIFICATIONS, CAMERA

## Message Protocol

All WebSocket messages are JSON:

```json
{ "type": "...", "payload": { ... } }
```

| Type               | Direction         | Description                          |
|--------------------|-------------------|--------------------------------------|
| `sms_list`         | phone → client    | Full conversation list               |
| `sms_thread`       | client → phone, phone → client | Request/response for thread messages |
| `sms_send`         | client → phone    | Send SMS `{ to, body }`              |
| `sms_incoming`     | phone → client    | New inbound SMS notification         |
| `read_receipt`     | client → phone    | Mark thread as read                  |
| `typing_indicator` | client → phone    | Typing notification                  |
| `mms_attachment`   | phone → client    | Base64 MMS attachment                |
| `contacts_list`    | phone → client    | Contact directory                    |
| `ping` / `pong`    | both              | 30-second keepalive                  |
| `phone_connected`  | server → client   | Phone joined the session             |
| `phone_disconnected` | server → client | Phone disconnected                   |
| `connection_status`| server → client   | Current phone connection state       |

## Session Flow

1. Web client calls `GET /new-session` → gets `sessionToken` + `pin` + `qrData`
2. Web client connects WebSocket as `role=client`
3. Web client polls `GET /session-status/{token}` every 2 seconds
4. Android app scans QR or enters PIN → connects WebSocket as `role=phone`
5. Server links phone ↔ client, notifies client via `phone_connected`
6. All messages relay bidirectionally in real time
