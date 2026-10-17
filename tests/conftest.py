import os

# Variables mínimas para que Settings no falle en tests.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test")
