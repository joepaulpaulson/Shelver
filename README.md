# Shelver

A WhatsApp chatbot that lets customers browse a store's catalog and place orders, right inside WhatsApp. No app needed. Built it for a supermarket use case first, but the logic isn't tied to groceries — swap the catalog data and FAQ text and it works for pretty much any small business doing product/service orders over WhatsApp (pharmacies, stationery shops, small restaurants, etc).

Built solo as a way to learn AWS properly and have something real to show for it.

## What it does

- Customer messages "Hi" → gets a menu of categories
- Browses items, sees price + stock
- Orders something → stock gets checked and updated, store owner gets a WhatsApp alert
- Can ask quick questions (hours, delivery, location) anytime
- Can ask for a human, and the bot goes quiet until reset

## How it works 

```
Customer's WhatsApp
      ↓
Meta WhatsApp Cloud API
      ↓ (webhook)
API Gateway → Lambda (Python)
      ↓
DynamoDB (products / conversations / orders)
```

Everything's serverless — no server running 24/7. Lambda only fires when someone messages the bot.

## Tables (DynamoDB)

- `shelver-products` — catalog items (name, category, price, stock)
- `shelver-conversations` — tracks where each customer is in the chat (main menu, browsing, etc)
- `shelver-orders` — every order placed
- `shelver-processed-messages` — stops duplicate orders if WhatsApp resends the same webhook

## Stack

- Python on AWS Lambda
- API Gateway (HTTP API) for the webhook
- DynamoDB for data
- SSM Parameter Store for the WhatsApp access token 
- Meta WhatsApp Cloud API

## Why some things were built the way they were

- **Serverless** — traffic is low and spiky, no point paying for an always-on server
- **Plain menu logic, not AI, for the core flow** — ordering needs to be exact every time. An LLM guessing what "I'll take three" means is cool but not something I want deciding my inventory numbers. AI is being added as a *fallback* for messages the menu logic can't understand, not replacing the core flow.
- **Stock updates are atomic** — used a DynamoDB conditional update so two people can't accidentally order the last item at the same time and both "succeed"

## Bugs I hit while building this (and actually learned something from)

- **OTP verification kept failing on desktop** — turned out to be a browser session issue, not the phone number. Worked instantly from mobile.
- **Webhook was "verified" but no messages ever came through** — spent a while confused because even Meta's own test-send tool didn't work. Turns out verifying the callback URL isn't enough — you also have to explicitly call `POST /{WABA_ID}/subscribed_apps` to actually link the app to receive events. Not obvious from the UI at all.
- **One blank string broke everything** — added a list of keywords for human handoff, and accidentally left an empty string `""` in there instead of `"help"`. In Python, `"" in any_string` is always `True`, so literally every message matched and got routed to "talk to a human" instead of the actual menu. One character, entire bot broken. Fixed by restoring the missing word.
- **Region mismatch** — created my DynamoDB tables in the wrong AWS region from my Lambda function. Fixed by just recreating them in the same region. Lesson: keep everything in one region unless you have a real reason not to.
- **Increased Security** - Added a signature verification to lambda to match against every webhook POST 

## Setup (if you want to run your own copy)

You'll need:
1. A Meta Developer account + WhatsApp test number
2. An AWS account
3. These environment variables set on the Lambda:
   - `PHONE_NUMBER_ID` — from your Meta WhatsApp setup
   - `OWNER_PHONE_NUMBER` — the number that gets order/handoff alerts
4. Your WhatsApp access token stored in SSM Parameter Store at `/shelver/whatsapp_token`

## What's next

Working through this roughly in order:
- [x] Core bot (browsing, ordering, FAQ, handoff) — done
- [x] Fixed stock/inventory bugs, duplicate order protection — done
- [ ] Infrastructure as code (so this isn't just console-clicking)
- [ ] AI fallback for messages the menu logic can't handle (AWS Bedrock)
- [ ] Simple dashboard for the store owner to see orders / edit stock
- [ ] Basic monitoring/alerts

## Things I'd do differently if I rebuilt this

- Split the one big Lambda file into smaller pieces (routing / orders / messaging) — fine at this size, won't stay fine if it grows
- Use a proper index instead of scanning the whole products table by category
- Real logging instead of scattered `print()` statements
