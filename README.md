# Telegram Bot

Telegram bot built with **Kurigram 2.2.17+**, Python, and MongoDB. It verifies required channel membership, registers users, and provides lookup actions from the persistent keyboard.

The persistent keyboard is split into three pages with **Next** and **Back** buttons, so each page shows no more than four actions.

## Commands

- `/start` — show bot instructions and register the user in MongoDB.
- `Number to Info` — select the keyboard button, then send a phone number to use the configured `NUM_TO_INFO` endpoint.
- `Aadhar to Info` — select the keyboard button, then send an Aadhaar number to use the configured `AADHAR_TO_INFO` endpoint.
- `Advanced Number Info` — select the keyboard button, then send a phone number to use `ADV_NUM_TO_INFO`.
- `PAN to Info` — select the keyboard button, then send a PAN to use `PAN_TO_INFO`.
- `Aadhaar Family` — select the keyboard button, then send an Aadhaar number to use `AADHAR_TO_FAMILY`.
- `IFSC to Info` — select the keyboard button, then send an IFSC code to use `IFSC_TO_INFO`.
- `Vehicle to Info` — premium users only; select the keyboard button, then send a vehicle registration number to receive the complete `VEHICLE_TO_INFO` JSON response.
- `Vehicle to Number` — premium users only; select the keyboard button, then send a vehicle registration number to use `VEHICLE_TO_NUMBER`.
- `TG to Info` — premium users only; select the keyboard button, then send a numeric Telegram user ID to use `TG_TO_INFO`.
- `/premium <userid> <dd-mm-yyyy>` — administrator-only command that grants unlimited lookups through the specified date.
- `/broadcast <message>` — administrator-only command that sends a formatted message to every registered user. To relay media or preserve formatting exactly, reply to the message with `/broadcast`.

Lookup data is formatted as individual records, with up to five records per Telegram message. Longer responses continue in additional messages.

Administrators can run `/protect <number>` to save a 10-digit mobile number in MongoDB's `protected_numbers` collection. A protected number is stopped before a credit is used or an external phone-lookup API is called; the user receives: `This number was protected. Try another number.`

## Setup

1. Install Python 3.10+ and MongoDB.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and add your Telegram API ID, API hash, bot token, and MongoDB URI. Configure `NUM_TO_INFO` for the number lookup button and `AADHAR_TO_INFO` for the Aadhaar lookup button.
   - Obtain `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
   - Create the bot token with [@BotFather](https://t.me/BotFather).
   - Set `CHANNEL_1_ID` to the private channel's numeric `-100...` ID, and add the bot as an **administrator** in both required channels. Keep the supplied invite URLs in `CHANNEL_1_URL` and `CHANNEL_2_URL`; Telegram cannot check membership using a private invite URL alone.
5. Start the bot:

   ```bash
   python main.py
   ```

The database stores Telegram user profiles in `users`. It saves minimal pending-request records in `pending_join_requests` only for invite links that require admin approval, so those users remain eligible after a bot restart. Direct-link joins are checked live and are not stored.

## Required group/channel check

On `/start`, users must join both configured groups or channels. The bot displays two join buttons and a **Joined** button; pressing it verifies their memberships before enabling commands. Users with a pending join request are allowed to use the bot, but their group/channel request is not automatically approved. The bot must be an administrator in every configured group/channel to receive and verify join requests.

## Privacy

Use the bot only with proper authorization, follow applicable laws, and secure MongoDB access.
