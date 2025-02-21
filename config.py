import os

#server envs
friendly_name = os.environ['FRIENDLY_NAME']
secret_key = os.environ['SECRET_KEY']

#limits
trigger_limit = int(os.environ.get('TRIGGER_LIMIT', 8))

#GPIO envs
pin = int(os.environ['GPIO_PIN'])
click_delay = float(os.environ.get('CLICK_DELAY', 0.10))

# API
api_key_expiration_days = int(os.environ.get('API_EXPIRATION_DAYS', 365))
admin_password = os.environ['ADMIN_PASSWORD']