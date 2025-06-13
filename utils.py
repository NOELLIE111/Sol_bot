import asyncio
import time
from loguru import logger

class APICounter:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(APICounter, cls).__new__(cls)
            cls._instance.request_timestamps = []
            asyncio.create_task(cls._instance.start_request_counter())
        return cls._instance

    async def start_request_counter(self):
        asyncio.create_task(self.log_request_count())

    async def log_request_count(self):
        while True:
            await asyncio.sleep(60)
            current_time = time.time()
            self.request_timestamps = [t for t in self.request_timestamps if current_time - t < 60]
            logger.debug(f"API-запросы за последнюю минуту: {len(self.request_timestamps)}")

    def record_request(self):
        self.request_timestamps.append(time.time())