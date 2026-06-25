import os
from celery import Celery

# ตั้งค่าโมดูล Settings ของ Django ให้เป็น Default สำหรับ Celery
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pea_project.settings")

# ตั้งชื่อแอป Celery ตามชื่อโปรเจกต์
app = Celery("pea_project")

# โหลดคอนฟิกจาก settings.py โดยใช้ตัวนำหน้าว่า CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# ให้ Celery ค้นหาไฟล์ tasks.py ในทุกๆ App อัตโนมัติ (เช่น oms/tasks.py)
app.autodiscover_tasks()
