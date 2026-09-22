
import json
import os
import uuid
import time
import hmac
import hashlib
import urllib.request
import urllib.error
import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

VERIFY_TOKEN = "shelver_v.0.1.2"
SSM_PARAM_NAME = "/shelver/whatsapp_token"
META_APP_SECRET_PARAM_NAME = "/shelver/meta_app_secret"
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]
OWNER_PHONE_NUMBER = os.environ["OWNER_PHONE_NUMBER"]
MAX_ORDER_QUANTITY = 10

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
products_table = dynamodb.Table("shelver-products")
conversations_table = dynamodb.Table("shelver-conversations")
orders_table = dynamodb.Table("shelver-orders")
processed_messages_table = dynamodb.Table("shelver-processed-messages")

CATEGORIES = ["Dairy", "Grocery", "Snacks", "Chocolates"]

FAQ_RESPONSES = {
    "hours": " We're open Mon–Sat, 8:00 AM – 9:00 PM. Closed Sundays.",
    "timing": " We're open Mon–Sat, 8:00 AM – 9:00 PM. Closed Sundays.",
    "delivery": " We deliver within 5km of the store, usually within 2 hours of ordering.",
    "location": " We're located at [Store Address Here]. Look for the Shelver banner out front!",
    "address": " We're located at [Store Address Here]. Look for the Shelver banner out front!",
    "return": " Items can be returned within 24 hours with the receipt, unopened.",
    "refund": " Items can be returned within 24 hours with the receipt, unopened."
}

HUMAN_KEYWORDS = ["human", "help", "agent", "support", "talk to someone"]

def verify_meta_signature(raw_body, signature_header):
    """Verify the request genuinely came from Meta using the X-Hub-Signature-256 header."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    app_secret = ssm.get_parameter(Name=META_APP_SECRET_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]

    expected_hash = hmac.new(
        app_secret.encode("utf-8"),
        raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body,
        hashlib.sha256
    ).hexdigest()

    received_hash = signature_header.split("sha256=")[1]

    return hmac.compare_digest(expected_hash, received_hash)

def get_access_token():
    response = ssm.get_parameter(Name=SSM_PARAM_NAME, WithDecryption=True)
    return response["Parameter"]["Value"]

def send_whatsapp_message(to_number, text):
    token = get_access_token()
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        print(f"Meta API rejected the request. Status: {e.code}, Body: {e.read().decode()}")
        raise

def is_duplicate_message(message_id):
    try:
        processed_messages_table.put_item(
            Item={"message_id": message_id, "processed_at": int(time.time())},
            ConditionExpression="attribute_not_exists(message_id)"
        )
        return False
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return True
        raise

def get_conversation_state(phone):
    result = conversations_table.get_item(Key={"phone_number": phone})
    if "Item" in result:
        return result["Item"]
    return {"phone_number": phone, "state": "main_menu"}

def save_conversation_state(phone, state, category=None, item_id=None):
    item = {"phone_number": phone, "state": state}
    if category:
        item["current_category"] = category
    if item_id:
        item["selected_item_id"] = item_id
    conversations_table.put_item(Item=item)

def get_products_by_category(category):
    response = products_table.scan(FilterExpression=Attr("Category").eq(category))
    return response.get("Items", [])

def get_product_by_id(product_id):
    response = products_table.get_item(Key={"product_id": product_id})
    return response.get("Item")

def build_main_menu():
    lines = ["Welcome to Shelver! 🛒", ""]
    for i, cat in enumerate(CATEGORIES, start=1):
        lines.append(f"{i}. {cat}")
    lines.append("\nReply with a number to browse.")
    lines.append("\nYou can also type: 'hours', 'delivery', 'location', or 'help' anytime.")
    return "\n".join(lines)

def build_category_list(category):
    items = get_products_by_category(category)
    if not items:
        return f"No items found in {category} right now.", items
    lines = [f"{category} items:", ""]
    for i, item in enumerate(items, start=1):
        stock = int(item.get("Stock", 0))
        tag = "" if stock > 0 else " (Out of stock)"
        lines.append(f"{i}. {item['Name']} - ₹{item['Price']}{tag}")
    lines.append("\nReply with a number for details, or 0 to go back.")
    return "\n".join(lines), items

def build_item_detail(item):
    stock = int(item.get("Stock", 0))
    stock_label = f"In stock ✅ ({stock} available)" if stock > 0 else "Out of stock ❌"
    reply = (f"🛍️ {item['Name']}\n"
             f"Category: {item['Category']}\n"
             f"Price: ₹{item['Price']}\n"
             f"{stock_label}\n\n")
    if stock > 0:
        reply += "Reply 'order' to buy this, or 0 to go back."
    else:
        reply += "This item is currently unavailable. Reply 0 to go back."
    return reply

def create_order_with_stock_check(customer_phone, item_id, quantity):
    item = get_product_by_id(item_id)
    if not item:
        return False, "That item no longer exists. Reply 'menu' to start over.", None, None, None

    current_stock = int(item.get("Stock", 0))

    if current_stock <= 0:
        return False, "Sorry, this item just went out of stock. Reply 0 to go back.", None, None, None

    if quantity > MAX_ORDER_QUANTITY:
        return False, f"Max order quantity is {MAX_ORDER_QUANTITY} per item. Please enter a smaller number.", None, None, None

    if quantity > current_stock:
        return False, f"Only {current_stock} left in stock. Please enter a smaller quantity.", None, None, None

    try:
        products_table.update_item(
            Key={"product_id": item_id},
            UpdateExpression="SET Stock = Stock - :qty",
            ConditionExpression="Stock >= :qty",
            ExpressionAttributeValues={":qty": quantity}
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False, "Sorry, stock changed while you were ordering. Please check availability again.", None, None, None
        raise

    order_id = str(uuid.uuid4())
    total = int(item["Price"]) * quantity
    orders_table.put_item(Item={
        "order_id": order_id,
        "customer_phone": customer_phone,
        "product_id": item["product_id"],
        "product_name": item["Name"],
        "quantity": quantity,
        "unit_price": int(item["Price"]),
        "total_price": total,
        "timestamp": int(time.time())
    })
    return True, None, order_id, item, total

def handle_human_handoff(sender, last_message):
    try:
        alert = f"🙋 Human help requested!\nFrom: {sender}\nMessage: {last_message}"
        send_whatsapp_message(OWNER_PHONE_NUMBER, alert)
    except Exception as e:
        print(f"Owner handoff notification failed: {e}")
    save_conversation_state(sender, "with_human")
    return "🙋 Got it — a store representative will reach out to you shortly. Reply 'menu' anytime to resume browsing with the bot."

def handle_message(sender, text):
    original_text = text.strip()
    text = original_text.lower()
    convo = get_conversation_state(sender)
    state = convo.get("state", "main_menu")

    if text in ["hi", "hello", "menu", "start"]:
        save_conversation_state(sender, "main_menu")
        return build_main_menu()

    if state == "with_human":
        return None

    if text in FAQ_RESPONSES:
        return FAQ_RESPONSES[text] + "\n\nType 'menu' to go back to browsing."

    if any(keyword in text for keyword in HUMAN_KEYWORDS):
        return handle_human_handoff(sender, original_text)

    if state == "main_menu":
        if text.isdigit() and 1 <= int(text) <= len(CATEGORIES):
            category = CATEGORIES[int(text) - 1]
            reply, _ = build_category_list(category)
            save_conversation_state(sender, "browsing_category", category)
            return reply
        return "Please reply with a valid number from the menu.\n\n" + build_main_menu()

    if state == "browsing_category":
        category = convo.get("current_category")
        if text == "0":
            save_conversation_state(sender, "main_menu")
            return build_main_menu()
        items = get_products_by_category(category)
        if text.isdigit() and 1 <= int(text) <= len(items):
            item = items[int(text) - 1]
            save_conversation_state(sender, "item_detail", category, item["product_id"])
            return build_item_detail(item)
        reply, _ = build_category_list(category)
        return "Please reply with a valid number.\n\n" + reply

    if state == "item_detail":
        category = convo.get("current_category")
        item_id = convo.get("selected_item_id")
        if text == "0":
            reply, _ = build_category_list(category)
            save_conversation_state(sender, "browsing_category", category)
            return reply
        if text == "order":
            item = get_product_by_id(item_id)
            if not item or int(item.get("Stock", 0)) <= 0:
                return "Sorry, this item is out of stock. Reply 0 to go back."
            save_conversation_state(sender, "awaiting_quantity", category, item_id)
            return "How many would you like? Reply with a number."
        return "Reply 'order' to buy this, or 0 to go back."

    if state == "awaiting_quantity":
        category = convo.get("current_category")
        item_id = convo.get("selected_item_id")
        if text.isdigit() and int(text) > 0:
            quantity = int(text)
            success, error_msg, order_id, item, total = create_order_with_stock_check(sender, item_id, quantity)

            if not success:
                return error_msg

            owner_msg = (f"🔔 New order!\n"
                         f"Item: {item['Name']}\n"
                         f"Qty: {quantity}\n"
                         f"Total: ₹{total}\n"
                         f"From: {sender}\n"
                         f"Order ID: {order_id[:8]}")
            try:
                send_whatsapp_message(OWNER_PHONE_NUMBER, owner_msg)
            except Exception as e:
                print(f"Owner notification failed: {e}")

            save_conversation_state(sender, "main_menu")
            return (f"✅ Order confirmed!\n{quantity} x {item['Name']} = ₹{total}\n\n"
                    f"We'll be in touch. Reply 'Hi' anytime to browse more.")
        return "Please reply with a valid quantity (a number)."

    save_conversation_state(sender, "main_menu")
    return build_main_menu()

def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if method == "GET":
        params = event.get("queryStringParameters") or {}
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": challenge}
        else:
            return {"statusCode": 403, "body": "Error, wrong validation token"}

    if method == "POST":
        try:
            raw_body = event.get("body", "")
            headers = event.get("headers", {}) or {}
            signature = headers.get("x-hub-signature-256")

            if not verify_meta_signature(raw_body, signature):
                print("Signature verification failed — rejecting request")
                return {"statusCode": 403, "body": "Invalid signature"}

            body = json.loads(raw_body or "{}")
            entry = body["entry"][0]
            change = entry["changes"][0]
            value = change["value"]

            if "messages" in value:
                message = value["messages"][0]
                message_id = message.get("id")
                sender = message["from"]
                incoming_text = message.get("text", {}).get("body", "")

                if message_id and is_duplicate_message(message_id):
                    print(f"Skipping duplicate message: {message_id}")
                else:
                    print(f"Received from {sender}: {incoming_text}")
                    reply_text = handle_message(sender, incoming_text)
                    if reply_text is not None:
                        send_whatsapp_message(sender, reply_text)

        except Exception as e:
            print(f"Error processing message: {e}")

        return {"statusCode": 200, "body": "EVENT_RECEIVED"}

    return {"statusCode": 200, "body": "OK"}
