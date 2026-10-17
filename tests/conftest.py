import os

# Variables mínimas para que Settings no falle en tests.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:AAHtest-token-for-unit-tests-0000000")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test")
