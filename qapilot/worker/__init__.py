"""Celery 분산 워커 — SaaS 멀티 인스턴스용.

기본은 비활성 (CELERY_ENABLED=false). 활성화 시 agent_router 가 asyncio.Task 대신
celery_app.send_task 로 파이프라인 제출. 실제 실행은 별도 worker 프로세스가 담당.

Author: C
Created: 2026-06-01
"""
