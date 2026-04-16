import os

class Config:
    DATA_PATH    = os.environ.get("DATA_PATH", "/data")
    CONFIG_PATH  = os.environ.get("CONFIG_PATH", "/config")
    PORT         = int(os.environ.get("PORT", "8080"))

config = Config()
