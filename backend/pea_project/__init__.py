# นำเข้า celery_app เพื่อให้มั่นใจว่าแอปถูกโหลดเสมอเมื่อ Django เริ่มทำงาน
from .celery import app as celery_app

__all__ = ("celery_app",)
