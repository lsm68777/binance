#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 Phoenix 95 V4 Ultimate - NOTIFY 서비스 (알림 허브)
================================================================================
📱 포트: 8103
🎯 역할: 텔레그램 알림, 실시간 대시보드, 성능 모니터링, 알림 통합 관리
⚡ 기능: 
  - 텔레그램 실시간 알림 (거래, 리스크, 시스템)
  - 웹 대시보드 (실시간 차트, 포지션 모니터링)
  - 성능 메트릭 수집/분석
  - 알림 큐 관리 및 배치 전송
  - 고가용성 알림 시스템
================================================================================
"""

import asyncio
import aiohttp
import json
import time
import logging
import os
import sys
import uuid
import hashlib
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from collections import deque
import numpy as np
import pandas as pd
import traceback
import threading
from pathlib import Path

# FastAPI 및 웹 관련
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 차트 및 시각화 (선택적 import)
CHART_AVAILABLE = False
try:
    import matplotlib
    matplotlib.use('Agg')  # GUI 없는 환경
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from io import BytesIO
    import base64
    CHART_AVAILABLE = True
    logging.info("📈 차트 라이브러리 로드 성공")
except ImportError:
    logging.warning("📈 matplotlib 없음 - 차트 기능 비활성화")

# Redis (선택적)
REDIS_AVAILABLE = False
try:
    import aioredis
    REDIS_AVAILABLE = True
    logging.info("💾 Redis 라이브러리 로드 성공")
except ImportError:
    logging.warning("💾 aioredis 없음 - Redis 기능 비활성화")

# PostgreSQL (선택적)
POSTGRES_AVAILABLE = False
try:
    import asyncpg
    POSTGRES_AVAILABLE = True
    logging.info("🗄️ PostgreSQL 라이브러리 로드 성공")
except ImportError:
    logging.warning("🗄️ asyncpg 없음 - PostgreSQL 기능 비활성화")

# ═══════════════════════════════════════════════════════════════════════════════
#                          🎯 V4 NOTIFY 시스템 설정  
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class NotifySystemConfig:
    """Phoenix 95 V4 NOTIFY 시스템 설정"""
    
    # 📱 텔레그램 설정 (검증된 값)
    TELEGRAM = {
        "bot_token": "7386542811:AAEZ21p30rES1k8NxNM2xbZ53U44PI9D5CY",
        "chat_id": "7590895952",
        "enabled": True,
        "rate_limit": 30,  # 초당 30개
        "retry_attempts": 3,
        "timeout": 10,
        "message_queue_size": 1000,
        "batch_size": 5,  # 5개씩 배치 전송
        "batch_interval": 2  # 2초마다 배치 전송
    }
    
    # 🌐 대시보드 설정
    DASHBOARD = {
        "host": "0.0.0.0",
        "port": 8103,
        "title": "Phoenix 95 V4 Ultimate - 알림 허브",
        "refresh_interval": 3,  # 3초마다 새로고침
        "theme": "dark",
        "enable_charts": CHART_AVAILABLE,  # matplotlib 가용성에 따라
        "enable_websocket": True,
        "max_data_points": 1000,
        "chart_history_hours": 24
    }
    
    # 📊 모니터링 설정
    MONITORING = {
        "metrics_interval": 5,  # 5초마다 메트릭 수집
        "alert_cooldown": 300,  # 5분 알림 쿨다운
        "performance_thresholds": {
            "cpu_percent": 80,
            "memory_percent": 85,
            "disk_percent": 90,
            "response_time_ms": 2000,
            "error_rate_percent": 5,
            "queue_size": 500
        },
        "health_check_interval": 30,  # 30초마다 헬스체크
        "auto_restart": True,
        "backup_interval": 3600  # 1시간마다 백업
    }
    
    # 🔄 큐 및 메시지 처리
    MESSAGE_PROCESSING = {
        "queue_size": 5000,
        "worker_count": 3,
        "batch_processing": True,
        "priority_levels": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
        "message_ttl": 3600,  # 1시간
        "dead_letter_queue": True,
        "retry_policy": {
            "max_retries": 5,
            "backoff_factor": 2,
            "initial_delay": 1
        }
    }
    
    # 📈 알림 설정
    ALERTS = {
        "trade_execution": True,
        "position_updates": True,
        "risk_warnings": True,
        "system_errors": True,
        "performance_alerts": True,
        "daily_summary": True,
        "liquidation_warnings": True,
        "profit_loss_updates": True,
        "market_alerts": True
    }
    
    # 💾 데이터 저장 설정
    DATA_STORAGE = {
        "redis_url": "redis://localhost:6379/1" if REDIS_AVAILABLE else None,
        "postgres_url": "postgresql://postgres:password@localhost:5432/phoenix95_v4" if POSTGRES_AVAILABLE else None,
        "backup_path": "C:/phoenix95_v4_ultimate/backups",
        "log_path": "C:/phoenix95_v4_ultimate/logs",
        "retention_days": 30,
        "compression": True
    }

# 전역 설정 인스턴스
config = NotifySystemConfig()

# ═══════════════════════════════════════════════════════════════════════════════
#                          📱 텔레그램 알림 관리자
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramNotificationManager:
    """텔레그램 알림 통합 관리자"""
    
    def __init__(self):
        self.config = config.TELEGRAM
        self.bot_token = self.config["bot_token"]
        self.chat_id = self.config["chat_id"]
        
        # 메시지 큐 (우선순위별)
        self.message_queues = {
            "CRITICAL": asyncio.Queue(maxsize=100),
            "HIGH": asyncio.Queue(maxsize=200),
            "MEDIUM": asyncio.Queue(maxsize=500),
            "LOW": asyncio.Queue(maxsize=1000)
        }
        
        # 통계 및 모니터링
        self.stats = {
            "total_sent": 0,
            "successful_sent": 0,
            "failed_sent": 0,
            "rate_limited": 0,
            "queue_overflow": 0,
            "last_sent_time": 0,
            "avg_response_time": 0.0
        }
        
        # 속도 제한 관리
        self.rate_limiter = asyncio.Semaphore(self.config["rate_limit"])
        self.last_sent_times = deque(maxlen=self.config["rate_limit"])
        
        # 메시지 템플릿
        self.templates = self._load_message_templates()
        
        # 알림 쿨다운 (중복 방지)
        self.alert_cooldowns = {}
        
        logging.info(f"📱 텔레그램 알림 관리자 초기화 완료 (속도제한: {self.config['rate_limit']}/초)")
    
    def _load_message_templates(self) -> Dict[str, str]:
        """메시지 템플릿 로드"""
        return {
            "trade_execution": """
🚀 <b>Phoenix 95 V4 - 거래 실행</b>

📊 <b>거래 정보</b>
• 심볼: <code>{symbol}</code>
• 액션: <b>{action}</b>
• 가격: <code>{price}</code>
• 레버리지: <b>{leverage}x ISOLATED</b>

💰 <b>포지션 정보</b>
• 포지션 크기: <code>{position_size}</code>
• 필요 마진: <code>{margin}</code>
• 청산가: <code>{liquidation_price}</code>

🎯 <b>분석 결과</b>
• Phoenix 95 점수: <b>{confidence:.1%}</b>
• 리스크 레벨: <b>{risk_level}</b>
• 실행 시간: <code>{execution_time:.0f}ms</code>

📈 <b>익절/손절 설정</b>
• 익절가: <code>{take_profit}</code> (+2%)
• 손절가: <code>{stop_loss}</code> (-2%)

⏰ {timestamp}
""",
            
            "position_update": """
📊 <b>포지션 업데이트</b>

🔄 <b>{symbol}</b> {action} {leverage}x
💰 현재가: <code>{current_price}</code>
📈 P&L: <b>{pnl}</b> ({pnl_percent:+.2f}%)
📊 ROE: <b>{roe:+.2f}%</b>

⚠️ 청산 위험도: <b>{liquidation_risk:.1%}</b>
🎯 상태: {status}

⏰ {timestamp}
""",
            
            "risk_warning": """
🚨 <b>리스크 경고</b>

⚠️ <b>경고 유형:</b> {warning_type}
📊 <b>심볼:</b> {symbol}
🔥 <b>위험도:</b> {risk_level}

📋 <b>상세 정보:</b>
{details}

🛡️ <b>권장 조치:</b>
{recommendations}

⏰ {timestamp}
""",
            
            "system_alert": """
🔧 <b>시스템 알림</b>

🎯 <b>서비스:</b> {service}
📊 <b>상태:</b> {status}
🔍 <b>메시지:</b> {message}

📈 <b>시스템 정보:</b>
• CPU: {cpu_percent:.1f}%
• 메모리: {memory_percent:.1f}%
• 활성 포지션: {active_positions}개

⏰ {timestamp}
""",
            
            "daily_summary": """
📊 <b>Phoenix 95 V4 - 일일 성과 요약</b>

💰 <b>거래 성과</b>
• 총 거래: <b>{total_trades}회</b>
• 총 P&L: <b>{total_pnl}</b>
• 승률: <b>{win_rate:.1f}%</b>
• 평균 ROE: <b>{avg_roe:+.2f}%</b>

📈 <b>최고 성과</b>
• 최고 수익: <b>{best_trade}</b>
• 최대 손실: <b>{worst_trade}</b>
• 최고 ROE: <b>{best_roe:+.2f}%</b>

⚡ <b>시스템 성능</b>
• 신호 처리: <b>{signals_processed}개</b>
• 평균 응답시간: <b>{avg_response_time:.0f}ms</b>
• 가동률: <b>{uptime_percent:.2f}%</b>

📊 <b>활성 포지션: {active_positions}개</b>
💸 <b>총 마진 사용: {total_margin}</b>

🗓️ {date}
""",
            
            "liquidation_warning": """
🆘 <b>청산 위험 경고</b>

📊 <b>{symbol}</b> {action} {leverage}x
💰 현재가: <code>{current_price}</code>
🚨 청산가: <code>{liquidation_price}</code>

⚠️ <b>위험도: {risk_level}</b>
📏 청산까지: <b>{distance_percent:.2f}%</b>
💔 예상 손실: <b>{estimated_loss}</b>

🛡️ <b>권장 조치:</b>
• 즉시 포지션 검토
• 추가 마진 고려
• 손절선 조정 검토

⏰ {timestamp}
""",
            
            "performance_alert": """
📈 <b>성능 알림</b>

🎯 <b>지표:</b> {metric}
📊 <b>현재 값:</b> {current_value}
⚠️ <b>임계값:</b> {threshold}
🔥 <b>심각도:</b> {severity}

📋 <b>영향:</b>
{impact_description}

🛠️ <b>권장 조치:</b>
{recommended_actions}

⏰ {timestamp}
"""
        }
    
    async def start_message_processing(self):
        """메시지 처리 시작"""
        logging.info("📱 텔레그램 메시지 처리 시작")
        
        # 우선순위별 워커 생성
        workers = []
        for priority in config.MESSAGE_PROCESSING["priority_levels"]:
            for i in range(config.MESSAGE_PROCESSING["worker_count"]):
                worker = asyncio.create_task(
                    self._message_worker(priority, f"{priority}_WORKER_{i+1}")
                )
                workers.append(worker)
        
        # 배치 처리 워커
        batch_worker = asyncio.create_task(self._batch_processing_worker())
        workers.append(batch_worker)
        
        logging.info(f"📱 {len(workers)}개 메시지 처리 워커 시작")
        return workers
    
    async def _message_worker(self, priority: str, worker_name: str):
        """메시지 처리 워커"""
        queue = self.message_queues[priority]
        
        while True:
            try:
                # 우선순위별 대기 시간 조정
                timeout = {"CRITICAL": 0.1, "HIGH": 0.5, "MEDIUM": 1.0, "LOW": 2.0}[priority]
                
                try:
                    message_data = await asyncio.wait_for(queue.get(), timeout=timeout)
                    
                    # 메시지 전송
                    success = await self._send_telegram_message(
                        message_data["text"], 
                        message_data.get("parse_mode", "HTML"),
                        message_data.get("disable_preview", True)
                    )
                    
                    if success:
                        self.stats["successful_sent"] += 1
                        logging.debug(f"📱 {worker_name}: 메시지 전송 성공")
                    else:
                        self.stats["failed_sent"] += 1
                        logging.warning(f"📱 {worker_name}: 메시지 전송 실패")
                    
                    queue.task_done()
                    
                except asyncio.TimeoutError:
                    # 타임아웃은 정상 - 다음 루프로
                    continue
                    
            except Exception as e:
                logging.error(f"📱 {worker_name} 오류: {e}")
                await asyncio.sleep(1)
    
    async def _batch_processing_worker(self):
        """배치 처리 워커"""
        batch_messages = []
        
        while True:
            try:
                # 배치 간격마다 실행
                await asyncio.sleep(self.config["batch_interval"])
                
                # 각 큐에서 메시지 수집
                for priority in config.MESSAGE_PROCESSING["priority_levels"]:
                    queue = self.message_queues[priority]
                    
                    # 배치 크기만큼 메시지 수집
                    for _ in range(min(self.config["batch_size"], queue.qsize())):
                        try:
                            message_data = queue.get_nowait()
                            batch_messages.append(message_data)
                        except asyncio.QueueEmpty:
                            break
                
                # 배치 메시지가 있으면 전송
                if batch_messages:
                    await self._send_batch_messages(batch_messages)
                    batch_messages.clear()
                    
            except Exception as e:
                logging.error(f"📱 배치 처리 오류: {e}")
                await asyncio.sleep(5)
    
    async def _send_batch_messages(self, messages: List[Dict]):
        """배치 메시지 전송"""
        if not messages:
            return
        
        try:
            # 메시지들을 하나로 결합
            combined_text = ""
            for i, msg in enumerate(messages):
                if i > 0:
                    combined_text += "\n" + "─" * 40 + "\n"
                combined_text += msg["text"]
            
            # 텔레그램 메시지 길이 제한 (4096자)
            if len(combined_text) > 4000:
                # 메시지를 나누어 전송
                chunks = self._split_message(combined_text, 4000)
                for chunk in chunks:
                    await self._send_telegram_message(chunk)
                    await asyncio.sleep(0.5)  # 속도 제한 방지
            else:
                await self._send_telegram_message(combined_text)
            
            logging.info(f"📱 배치 전송 완료: {len(messages)}개 메시지")
            
        except Exception as e:
            logging.error(f"📱 배치 전송 실패: {e}")
    
    def _split_message(self, text: str, max_length: int) -> List[str]:
        """메시지 분할"""
        chunks = []
        current_chunk = ""
        
        for line in text.split('\n'):
            if len(current_chunk + line + '\n') <= max_length:
                current_chunk += line + '\n'
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks
    
    async def _send_telegram_message(self, text: str, parse_mode: str = "HTML", 
                                   disable_preview: bool = True) -> bool:
        """텔레그램 메시지 전송"""
        if not self.config["enabled"]:
            return False
        
        try:
            # 속도 제한 체크
            await self._check_rate_limit()
            
            start_time = time.time()
            
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=self.config["timeout"]) as response:
                    response_time = (time.time() - start_time) * 1000
                    
                    # 응답 시간 통계 업데이트
                    self._update_response_time_stats(response_time)
                    
                    if response.status == 200:
                        self.stats["total_sent"] += 1
                        self.stats["last_sent_time"] = time.time()
                        return True
                    elif response.status == 429:
                        # 속도 제한
                        self.stats["rate_limited"] += 1
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logging.warning(f"📱 텔레그램 속도 제한: {retry_after}초 대기")
                        await asyncio.sleep(retry_after)
                        return False
                    else:
                        logging.error(f"📱 텔레그램 오류: HTTP {response.status}")
                        return False
        
        except asyncio.TimeoutError:
            logging.warning("📱 텔레그램 전송 타임아웃")
            return False
        except Exception as e:
            logging.error(f"📱 텔레그램 전송 실패: {e}")
            return False
    
    async def _check_rate_limit(self):
        """속도 제한 체크"""
        async with self.rate_limiter:
            current_time = time.time()
            
            # 1초 이내 전송된 메시지 수 체크
            while (self.last_sent_times and 
                   current_time - self.last_sent_times[0] < 1.0 and
                   len(self.last_sent_times) >= self.config["rate_limit"]):
                await asyncio.sleep(0.1)
                current_time = time.time()
            
            self.last_sent_times.append(current_time)
    
    def _update_response_time_stats(self, response_time: float):
        """응답 시간 통계 업데이트"""
        total_requests = self.stats["total_sent"] + 1
        current_avg = self.stats["avg_response_time"]
        
        self.stats["avg_response_time"] = (
            (current_avg * (total_requests - 1) + response_time) / total_requests
        )
    
    async def send_trade_notification(self, trade_data: Dict, priority: str = "HIGH"):
        """거래 알림 전송"""
        try:
            message_text = self.templates["trade_execution"].format(
                symbol=trade_data.get("symbol", "UNKNOWN"),
                action=trade_data.get("action", "UNKNOWN").upper(),
                price=trade_data.get("price", 0),
                leverage=trade_data.get("leverage", 1),
                position_size=trade_data.get("position_size", 0),
                margin=trade_data.get("margin_required", 0),
                liquidation_price=trade_data.get("liquidation_price", 0),
                confidence=trade_data.get("confidence", 0),
                risk_level=trade_data.get("risk_level", "MEDIUM"),
                execution_time=trade_data.get("execution_time_ms", 0),
                take_profit=trade_data.get("take_profit_price", 0),
                stop_loss=trade_data.get("stop_loss_price", 0),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 거래 알림 생성 실패: {e}")
    
    async def send_position_update(self, position_data: Dict, priority: str = "MEDIUM"):
        """포지션 업데이트 알림"""
        try:
            message_text = self.templates["position_update"].format(
                symbol=position_data.get("symbol", "UNKNOWN"),
                action=position_data.get("action", "UNKNOWN").upper(),
                leverage=position_data.get("leverage", 1),
                current_price=position_data.get("current_price", 0),
                pnl=position_data.get("unrealized_pnl", 0),
                pnl_percent=position_data.get("pnl_percentage", 0),
                roe=position_data.get("roe", 0),
                liquidation_risk=position_data.get("liquidation_risk", 0),
                status=position_data.get("status", "ACTIVE"),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 포지션 업데이트 알림 생성 실패: {e}")
    
    async def send_risk_warning(self, risk_data: Dict, priority: str = "CRITICAL"):
        """리스크 경고 알림"""
        try:
            message_text = self.templates["risk_warning"].format(
                warning_type=risk_data.get("warning_type", "UNKNOWN"),
                symbol=risk_data.get("symbol", "UNKNOWN"),
                risk_level=risk_data.get("risk_level", "HIGH"),
                details=risk_data.get("details", "상세 정보 없음"),
                recommendations=risk_data.get("recommendations", "즉시 확인 필요"),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 리스크 경고 알림 생성 실패: {e}")
    
    async def send_system_alert(self, alert_data: Dict, priority: str = "HIGH"):
        """시스템 알림 전송"""
        try:
            message_text = self.templates["system_alert"].format(
                service=alert_data.get("service", "UNKNOWN"),
                status=alert_data.get("status", "UNKNOWN"),
                message=alert_data.get("message", "메시지 없음"),
                cpu_percent=alert_data.get("cpu_percent", 0),
                memory_percent=alert_data.get("memory_percent", 0),
                active_positions=alert_data.get("active_positions", 0),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 시스템 알림 생성 실패: {e}")
    
    async def send_daily_summary(self, summary_data: Dict, priority: str = "LOW"):
        """일일 요약 알림"""
        try:
            message_text = self.templates["daily_summary"].format(
                total_trades=summary_data.get("total_trades", 0),
                total_pnl=summary_data.get("total_pnl", 0),
                win_rate=summary_data.get("win_rate", 0),
                avg_roe=summary_data.get("avg_roe", 0),
                best_trade=summary_data.get("best_trade", 0),
                worst_trade=summary_data.get("worst_trade", 0),
                best_roe=summary_data.get("best_roe", 0),
                signals_processed=summary_data.get("signals_processed", 0),
                avg_response_time=summary_data.get("avg_response_time", 0),
                uptime_percent=summary_data.get("uptime_percent", 0),
                active_positions=summary_data.get("active_positions", 0),
                total_margin=summary_data.get("total_margin", 0),
                date=datetime.now().strftime('%Y-%m-%d')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 일일 요약 알림 생성 실패: {e}")
    
    async def send_liquidation_warning(self, liquidation_data: Dict, priority: str = "CRITICAL"):
        """청산 위험 경고"""
        try:
            message_text = self.templates["liquidation_warning"].format(
                symbol=liquidation_data.get("symbol", "UNKNOWN"),
                action=liquidation_data.get("action", "UNKNOWN").upper(),
                leverage=liquidation_data.get("leverage", 1),
                current_price=liquidation_data.get("current_price", 0),
                liquidation_price=liquidation_data.get("liquidation_price", 0),
                risk_level=liquidation_data.get("risk_level", "CRITICAL"),
                distance_percent=liquidation_data.get("distance_percent", 0),
                estimated_loss=liquidation_data.get("estimated_loss", 0),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 청산 경고 알림 생성 실패: {e}")
    
    async def send_performance_alert(self, perf_data: Dict, priority: str = "MEDIUM"):
        """성능 알림 전송"""
        try:
            message_text = self.templates["performance_alert"].format(
                metric=perf_data.get("metric", "UNKNOWN"),
                current_value=perf_data.get("current_value", "N/A"),
                threshold=perf_data.get("threshold", "N/A"),
                severity=perf_data.get("severity", "MEDIUM"),
                impact_description=perf_data.get("impact_description", "영향 분석 중"),
                recommended_actions=perf_data.get("recommended_actions", "모니터링 계속"),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
            
            await self._queue_message(message_text, priority)
            
        except Exception as e:
            logging.error(f"📱 성능 알림 생성 실패: {e}")
    
    async def _queue_message(self, text: str, priority: str = "MEDIUM"):
        """메시지 큐에 추가"""
        try:
            # 중복 알림 방지 (쿨다운 체크)
            message_hash = hashlib.md5(text.encode()).hexdigest()
            current_time = time.time()
            
            if message_hash in self.alert_cooldowns:
                if current_time - self.alert_cooldowns[message_hash] < config.MONITORING["alert_cooldown"]:
                    logging.debug(f"📱 중복 알림 차단: {message_hash[:8]}")
                    return
            
            self.alert_cooldowns[message_hash] = current_time
            
            # 우선순위 큐에 추가
            queue = self.message_queues.get(priority, self.message_queues["MEDIUM"])
            
            message_data = {
                "text": text,
                "priority": priority,
                "timestamp": current_time,
                "parse_mode": "HTML",
                "disable_preview": True
            }
            
            try:
                queue.put_nowait(message_data)
                logging.debug(f"📱 메시지 큐 추가: {priority} 우선순위")
            except asyncio.QueueFull:
                self.stats["queue_overflow"] += 1
                logging.warning(f"📱 {priority} 큐 오버플로우")
        
        except Exception as e:
            logging.error(f"📱 메시지 큐 추가 실패: {e}")
    
    def get_notification_stats(self) -> Dict:
        """알림 통계 조회"""
        queue_sizes = {
            priority: queue.qsize() 
            for priority, queue in self.message_queues.items()
        }
        
        return {
            "stats": self.stats.copy(),
            "queue_sizes": queue_sizes,
            "total_queue_size": sum(queue_sizes.values()),
            "rate_limit_remaining": self.config["rate_limit"] - len(self.last_sent_times),
            "cooldown_entries": len(self.alert_cooldowns),
            "enabled": self.config["enabled"]
        }

# ═══════════════════════════════════════════════════════════════════════════════
#                          📊 성능 모니터링 시스템
# ═══════════════════════════════════════════════════════════════════════════════

class PerformanceMonitor:
    """통합 성능 모니터링 시스템"""
    
    def __init__(self, telegram_manager: TelegramNotificationManager):
        self.telegram = telegram_manager
        self.config_monitor = config.MONITORING
        
        # 메트릭 저장소
        self.metrics_history = deque(maxlen=self.config_monitor["metrics_interval"] * 1440)  # 24시간
        self.alert_history = deque(maxlen=1000)
        
        # 서비스 상태 추적
        self.service_states = {
            "brain_service": {"url": "http://localhost:8100/health", "status": "unknown"},
            "risk_service": {"url": "http://localhost:8101/health", "status": "unknown"},
            "execute_service": {"url": "http://localhost:8102/health", "status": "unknown"},
            "notify_service": {"url": "http://localhost:8103/health", "status": "healthy"}
        }
        
        # 성능 통계
        self.performance_stats = {
            "start_time": time.time(),
            "total_checks": 0,
            "alerts_sent": 0,
            "avg_response_time": 0.0,
            "uptime_percent": 100.0,
            "last_alert_time": 0
        }
        
        logging.info("📊 성능 모니터링 시스템 초기화 완료")
    
    async def start_monitoring(self):
        """모니터링 시작"""
        logging.info("📊 통합 성능 모니터링 시작")
        
        tasks = [
            asyncio.create_task(self._system_metrics_collector()),
            asyncio.create_task(self._service_health_checker()),
            asyncio.create_task(self._alert_processor()),
            asyncio.create_task(self._performance_analyzer())
        ]
        
        return tasks
    
    async def _system_metrics_collector(self):
        """시스템 메트릭 수집기"""
        while True:
            try:
                metrics = await self._collect_system_metrics()
                self.metrics_history.append(metrics)
                
                # 임계값 체크 및 알림
                await self._check_thresholds(metrics)
                
                await asyncio.sleep(self.config_monitor["metrics_interval"])
                
            except Exception as e:
                logging.error(f"📊 시스템 메트릭 수집 오류: {e}")
                await asyncio.sleep(10)
    
    async def _collect_system_metrics(self) -> Dict:
        """시스템 메트릭 수집"""
        try:
            # 시스템 리소스
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # 네트워크 통계
            network = psutil.net_io_counters()
            
            # 프로세스 정보
            process = psutil.Process()
            process_memory = process.memory_info().rss / 1024 / 1024  # MB
            process_cpu = process.cpu_percent()
            
            # 스레드 및 연결 수
            thread_count = threading.active_count()
            
            return {
                "timestamp": time.time(),
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_gb": memory.available / (1024**3),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024**3),
                "network_bytes_sent": network.bytes_sent,
                "network_bytes_recv": network.bytes_recv,
                "process_memory_mb": process_memory,
                "process_cpu_percent": process_cpu,
                "thread_count": thread_count,
                "active_connections": 0  # FastAPI에서 별도 수집
            }
            
        except Exception as e:
            logging.error(f"📊 메트릭 수집 실패: {e}")
            return {"timestamp": time.time(), "error": str(e)}
    
    async def _service_health_checker(self):
        """서비스 헬스체크"""
        while True:
            try:
                for service_name, service_info in self.service_states.items():
                    if service_name == "notify_service":
                        # 자기 자신은 건강함
                        service_info["status"] = "healthy"
                        service_info["response_time"] = 0
                        continue
                    
                    # 다른 서비스 헬스체크
                    try:
                        start_time = time.time()
                        
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                service_info["url"], 
                                timeout=aiohttp.ClientTimeout(total=5)
                            ) as response:
                                response_time = (time.time() - start_time) * 1000
                                
                                if response.status == 200:
                                    service_info["status"] = "healthy"
                                    service_info["response_time"] = response_time
                                else:
                                    service_info["status"] = "unhealthy"
                                    service_info["response_time"] = response_time
                    
                    except asyncio.TimeoutError:
                        service_info["status"] = "timeout"
                        service_info["response_time"] = 5000
                    except Exception as e:
                        service_info["status"] = "error"
                        service_info["error"] = str(e)
                        service_info["response_time"] = 0
                
                # 서비스 상태 변화 알림
                await self._check_service_status_changes()
                
                await asyncio.sleep(self.config_monitor["health_check_interval"])
                
            except Exception as e:
                logging.error(f"📊 서비스 헬스체크 오류: {e}")
                await asyncio.sleep(30)
    
    async def _check_thresholds(self, metrics: Dict):
        """임계값 체크 및 알림"""
        if "error" in metrics:
            return
        
        thresholds = self.config_monitor["performance_thresholds"]
        alerts = []
        
        # CPU 체크
        if metrics["cpu_percent"] > thresholds["cpu_percent"]:
            alerts.append({
                "type": "CPU_HIGH",
                "metric": "CPU 사용률",
                "current_value": f"{metrics['cpu_percent']:.1f}%",
                "threshold": f"{thresholds['cpu_percent']}%",
                "severity": "HIGH" if metrics["cpu_percent"] > 90 else "MEDIUM"
            })
        
        # 메모리 체크
        if metrics["memory_percent"] > thresholds["memory_percent"]:
            alerts.append({
                "type": "MEMORY_HIGH",
                "metric": "메모리 사용률",
                "current_value": f"{metrics['memory_percent']:.1f}%",
                "threshold": f"{thresholds['memory_percent']}%",
                "severity": "HIGH" if metrics["memory_percent"] > 95 else "MEDIUM"
            })
        
        # 디스크 체크
        if metrics["disk_percent"] > thresholds["disk_percent"]:
            alerts.append({
                "type": "DISK_HIGH",
                "metric": "디스크 사용률",
                "current_value": f"{metrics['disk_percent']:.1f}%",
                "threshold": f"{thresholds['disk_percent']}%",
                "severity": "MEDIUM"
            })
        
        # 프로세스 메모리 체크
        if metrics["process_memory_mb"] > 1000:  # 1GB
            alerts.append({
                "type": "PROCESS_MEMORY_HIGH",
                "metric": "프로세스 메모리",
                "current_value": f"{metrics['process_memory_mb']:.0f}MB",
                "threshold": "1000MB",
                "severity": "LOW"
            })
        
        # 스레드 수 체크
        if metrics["thread_count"] > 50:
            alerts.append({
                "type": "THREAD_COUNT_HIGH",
                "metric": "활성 스레드 수",
                "current_value": f"{metrics['thread_count']}개",
                "threshold": "50개",
                "severity": "LOW"
            })
        
        # 알림 전송
        for alert in alerts:
            await self._send_performance_alert(alert)
    
    async def _send_performance_alert(self, alert_data: Dict):
        """성능 알림 전송"""
        try:
            # 중복 알림 방지
            alert_key = f"{alert_data['type']}_{alert_data['severity']}"
            current_time = time.time()
            
            if (self.performance_stats["last_alert_time"] > 0 and
                current_time - self.performance_stats["last_alert_time"] < 300):  # 5분 쿨다운
                return
            
            impact_descriptions = {
                "CPU_HIGH": "시스템 응답 속도 저하, 거래 실행 지연 가능",
                "MEMORY_HIGH": "메모리 부족으로 인한 시스템 불안정성 증가",
                "DISK_HIGH": "로그 및 데이터 저장 공간 부족",
                "PROCESS_MEMORY_HIGH": "프로세스 메모리 사용량 증가",
                "THREAD_COUNT_HIGH": "과도한 스레드로 인한 성능 저하"
            }
            
            recommended_actions = {
                "CPU_HIGH": "• 불필요한 프로세스 종료\n• 시스템 리소스 확인\n• 서버 성능 업그레이드 검토",
                "MEMORY_HIGH": "• 메모리 정리 실행\n• 애플리케이션 재시작 검토\n• 메모리 증설 검토",
                "DISK_HIGH": "• 로그 파일 정리\n• 임시 파일 삭제\n• 디스크 용량 확장",
                "PROCESS_MEMORY_HIGH": "• 애플리케이션 메모리 사용량 모니터링\n• 메모리 누수 검사",
                "THREAD_COUNT_HIGH": "• 스레드 풀 설정 검토\n• 동시성 제한 확인"
            }
            
            perf_data = {
                "metric": alert_data["metric"],
                "current_value": alert_data["current_value"],
                "threshold": alert_data["threshold"],
                "severity": alert_data["severity"],
                "impact_description": impact_descriptions.get(alert_data["type"], "성능에 영향을 줄 수 있음"),
                "recommended_actions": recommended_actions.get(alert_data["type"], "시스템 모니터링 계속")
            }
            
            await self.telegram.send_performance_alert(perf_data, alert_data["severity"])
            
            self.performance_stats["alerts_sent"] += 1
            self.performance_stats["last_alert_time"] = current_time
            
            self.alert_history.append({
                "timestamp": current_time,
                "type": alert_data["type"],
                "severity": alert_data["severity"],
                "data": alert_data
            })
            
        except Exception as e:
            logging.error(f"📊 성능 알림 전송 실패: {e}")
    
    async def _check_service_status_changes(self):
        """서비스 상태 변화 체크"""
        try:
            unhealthy_services = []
            
            for service_name, service_info in self.service_states.items():
                if service_info["status"] not in ["healthy", "unknown"]:
                    unhealthy_services.append({
                        "service": service_name,
                        "status": service_info["status"],
                        "error": service_info.get("error", "상태 확인 실패")
                    })
            
            if unhealthy_services:
                # 서비스 다운 알림
                for service in unhealthy_services:
                    alert_data = {
                        "service": service["service"],
                        "status": service["status"],
                        "message": service["error"],
                        "cpu_percent": 0,
                        "memory_percent": 0,
                        "active_positions": 0
                    }
                    
                    await self.telegram.send_system_alert(alert_data, "CRITICAL")
            
        except Exception as e:
            logging.error(f"📊 서비스 상태 변화 체크 실패: {e}")
    
    async def _alert_processor(self):
        """알림 처리기"""
        while True:
            try:
                # 주기적으로 시스템 상태 요약 전송
                await asyncio.sleep(3600)  # 1시간마다
                
                # 시간별 요약 생성
                await self._send_hourly_summary()
                
            except Exception as e:
                logging.error(f"📊 알림 처리 오류: {e}")
                await asyncio.sleep(60)
    
    async def _performance_analyzer(self):
        """성능 분석기"""
        while True:
            try:
                await asyncio.sleep(300)  # 5분마다
                
                if len(self.metrics_history) < 10:
                    continue
                
                # 최근 데이터 분석
                recent_metrics = list(self.metrics_history)[-60:]  # 최근 5분
                
                # 트렌드 분석
                cpu_trend = self._calculate_trend([m.get("cpu_percent", 0) for m in recent_metrics])
                memory_trend = self._calculate_trend([m.get("memory_percent", 0) for m in recent_metrics])
                
                # 트렌드 기반 예측 알림
                if cpu_trend > 2.0:  # 증가 추세
                    logging.warning(f"📊 CPU 사용률 증가 추세 감지: {cpu_trend:.2f}%/분")
                
                if memory_trend > 1.0:  # 메모리 증가 추세
                    logging.warning(f"📊 메모리 사용률 증가 추세 감지: {memory_trend:.2f}%/분")
                
            except Exception as e:
                logging.error(f"📊 성능 분석 오류: {e}")
                await asyncio.sleep(60)
    
    def _calculate_trend(self, values: List[float]) -> float:
        """트렌드 계산 (선형 회귀)"""
        if len(values) < 5:
            return 0.0
        
        try:
            x = np.arange(len(values))
            y = np.array(values)
            
            # 선형 회귀로 기울기 계산
            slope, _ = np.polyfit(x, y, 1)
            return slope
        except:
            return 0.0
    
    async def _send_hourly_summary(self):
        """시간별 요약 전송"""
        try:
            if not self.metrics_history:
                return
            
            # 최근 1시간 데이터
            hour_ago = time.time() - 3600
            recent_metrics = [m for m in self.metrics_history if m["timestamp"] > hour_ago]
            
            if not recent_metrics:
                return
            
            # 평균 계산
            avg_cpu = np.mean([m.get("cpu_percent", 0) for m in recent_metrics])
            avg_memory = np.mean([m.get("memory_percent", 0) for m in recent_metrics])
            avg_disk = np.mean([m.get("disk_percent", 0) for m in recent_metrics])
            
            # 서비스 상태 요약
            healthy_services = sum(1 for s in self.service_states.values() if s["status"] == "healthy")
            total_services = len(self.service_states)
            
            alert_data = {
                "service": "NOTIFY 허브",
                "status": "정상 운영",
                "message": f"시간별 성능 요약\nCPU: {avg_cpu:.1f}% | 메모리: {avg_memory:.1f}% | 디스크: {avg_disk:.1f}%\n건강한 서비스: {healthy_services}/{total_services}",
                "cpu_percent": avg_cpu,
                "memory_percent": avg_memory,
                "active_positions": 0
            }
            
            await self.telegram.send_system_alert(alert_data, "LOW")
            
        except Exception as e:
            logging.error(f"📊 시간별 요약 전송 실패: {e}")
    
    def get_monitoring_stats(self) -> Dict:
        """모니터링 통계 조회"""
        if not self.metrics_history:
            return {"error": "메트릭 데이터 없음"}
        
        recent_metrics = list(self.metrics_history)[-60:]  # 최근 5분
        
        return {
            "system_metrics": {
                "avg_cpu_percent": np.mean([m.get("cpu_percent", 0) for m in recent_metrics]),
                "avg_memory_percent": np.mean([m.get("memory_percent", 0) for m in recent_metrics]),
                "avg_disk_percent": np.mean([m.get("disk_percent", 0) for m in recent_metrics]),
                "thread_count": recent_metrics[-1].get("thread_count", 0) if recent_metrics else 0
            },
            "service_status": self.service_states.copy(),
            "performance_stats": self.performance_stats.copy(),
            "alert_count": len(self.alert_history),
            "metrics_count": len(self.metrics_history),
            "uptime_seconds": time.time() - self.performance_stats["start_time"]
        }

# ═══════════════════════════════════════════════════════════════════════════════
#                          🌐 실시간 웹 대시보드
# ═══════════════════════════════════════════════════════════════════════════════

class RealTimeDashboard:
    """실시간 웹 대시보드"""
    
    def __init__(self, telegram_manager: TelegramNotificationManager, 
                 performance_monitor: PerformanceMonitor):
        self.telegram = telegram_manager
        self.monitor = performance_monitor
        self.config_dash = config.DASHBOARD
        
        # WebSocket 연결 관리
        self.active_connections: List[WebSocket] = []
        
        # 차트 데이터 캐시
        self.chart_data_cache = {}
        self.last_chart_update = 0
        
        logging.info("🌐 실시간 대시보드 초기화 완료")
    
    def generate_dashboard_html(self) -> str:
        """대시보드 HTML 생성"""
        
        # 통계 데이터 수집
        telegram_stats = self.telegram.get_notification_stats()
        monitoring_stats = self.monitor.get_monitoring_stats()
        
        return f"""
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.config_dash["title"]}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 50%, #16213e 100%);
            color: #ffffff;
            min-height: 100vh;
            padding: 20px;
        }}
        
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 15px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .header h1 {{
            font-size: 2.5em;
            color: #00ff88;
            text-shadow: 0 0 20px rgba(0, 255, 136, 0.5);
            margin-bottom: 10px;
        }}
        
        .status-indicator {{
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }}
        
        .status-healthy {{ background: #00ff88; }}
        .status-warning {{ background: #ffaa00; }}
        .status-critical {{ background: #ff4444; }}
        
        @keyframes pulse {{
            0% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
            100% {{ opacity: 1; }}
        }}
        
        .dashboard-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        
        .card {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 15px;
            padding: 25px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }}
        
        .card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0, 255, 136, 0.2);
        }}
        
        .card-title {{
            font-size: 1.4em;
            color: #00ff88;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .card-icon {{
            font-size: 1.5em;
        }}
        
        .metric-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .metric-item:last-child {{
            border-bottom: none;
        }}
        
        .metric-label {{
            color: #cccccc;
            font-weight: 500;
        }}
        
        .metric-value {{
            color: #00ff88;
            font-weight: bold;
            font-size: 1.1em;
        }}
        
        .metric-value.warning {{
            color: #ffaa00;
        }}
        
        .metric-value.critical {{
            color: #ff4444;
        }}
        
        .queue-status {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }}
        
        .queue-item {{
            background: rgba(0, 255, 136, 0.1);
            border-radius: 10px;
            padding: 15px;
            text-align: center;
            border: 1px solid rgba(0, 255, 136, 0.3);
        }}
        
        .queue-priority {{
            font-size: 0.9em;
            color: #cccccc;
            margin-bottom: 5px;
        }}
        
        .queue-count {{
            font-size: 1.5em;
            color: #00ff88;
            font-weight: bold;
        }}
        
        .footer {{
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            color: #888;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .auto-refresh {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(0, 255, 136, 0.2);
            border: 1px solid #00ff88;
            border-radius: 20px;
            padding: 8px 15px;
            color: #00ff88;
            font-size: 0.9em;
        }}
        
        @media (max-width: 768px) {{
            .dashboard-grid {{
                grid-template-columns: 1fr;
            }}
            
            .header h1 {{
                font-size: 2em;
            }}
            
            .card {{
                padding: 20px;
            }}
        }}
        
        .connection-status {{
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 10px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            font-weight: bold;
        }}
        
        .connected {{
            background: rgba(0, 255, 136, 0.2);
            border: 1px solid #00ff88;
            color: #00ff88;
        }}
        
        .disconnected {{
            background: rgba(255, 68, 68, 0.2);
            border: 1px solid #ff4444;
            color: #ff4444;
        }}
        
        .info-banner {{
            background: rgba(255, 170, 0, 0.1);
            border: 1px solid #ffaa00;
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 20px;
            text-align: center;
        }}
        
        .info-banner .icon {{
            font-size: 1.2em;
            margin-right: 8px;
        }}
    </style>
</head>
<body>
    <div class="auto-refresh" id="refresh-indicator">
        🔄 자동 새로고침: {self.config_dash["refresh_interval"]}초
    </div>
    
    <div class="connection-status connected" id="connection-status">
        <span class="status-indicator status-healthy"></span>실시간 연결
    </div>
    
    <div class="header">
        <h1>🚀 Phoenix 95 V4 Ultimate</h1>
        <h2>📱 NOTIFY 서비스 - 알림 허브</h2>
        <p><span class="status-indicator status-healthy"></span>포트 8103 | 실시간 모니터링 및 알림 관리</p>
        <p>마지막 업데이트: <span id="last-update">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</span></p>
    </div>
    
    {f'''
    <div class="info-banner">
        <span class="icon">⚠️</span>
        <strong>차트 기능 비활성화:</strong> matplotlib 라이브러리가 설치되지 않았습니다. 
        <code>pip install matplotlib</code>로 설치하면 실시간 차트를 볼 수 있습니다.
    </div>
    ''' if not CHART_AVAILABLE else ''}
    
    <div class="dashboard-grid">
        <!-- 텔레그램 알림 통계 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">📱</span>
                텔레그램 알림 통계
            </div>
            <div class="metric-item">
                <span class="metric-label">총 전송:</span>
                <span class="metric-value">{telegram_stats['stats']['total_sent']:,}개</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">성공 전송:</span>
                <span class="metric-value">{telegram_stats['stats']['successful_sent']:,}개</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">실패 전송:</span>
                <span class="metric-value {'warning' if telegram_stats['stats']['failed_sent'] > 0 else ''}">{telegram_stats['stats']['failed_sent']:,}개</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">속도 제한:</span>
                <span class="metric-value {'warning' if telegram_stats['stats']['rate_limited'] > 0 else ''}">{telegram_stats['stats']['rate_limited']:,}회</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">평균 응답시간:</span>
                <span class="metric-value">{telegram_stats['stats']['avg_response_time']:.0f}ms</span>
            </div>
        </div>
        
        <!-- 메시지 큐 상태 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">📬</span>
                메시지 큐 상태
            </div>
            <div class="metric-item">
                <span class="metric-label">전체 대기:</span>
                <span class="metric-value">{telegram_stats['total_queue_size']:,}개</span>
            </div>
            <div class="queue-status">
                <div class="queue-item">
                    <div class="queue-priority">CRITICAL</div>
                    <div class="queue-count">{telegram_stats['queue_sizes']['CRITICAL']}</div>
                </div>
                <div class="queue-item">
                    <div class="queue-priority">HIGH</div>
                    <div class="queue-count">{telegram_stats['queue_sizes']['HIGH']}</div>
                </div>
                <div class="queue-item">
                    <div class="queue-priority">MEDIUM</div>
                    <div class="queue-count">{telegram_stats['queue_sizes']['MEDIUM']}</div>
                </div>
                <div class="queue-item">
                    <div class="queue-priority">LOW</div>
                    <div class="queue-count">{telegram_stats['queue_sizes']['LOW']}</div>
                </div>
            </div>
        </div>
        
        <!-- 시스템 성능 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">📊</span>
                시스템 성능
            </div>
            <div class="metric-item">
                <span class="metric-label">CPU 사용률:</span>
                <span class="metric-value {'warning' if monitoring_stats.get('system_metrics', {}).get('avg_cpu_percent', 0) > 70 else ''}">{monitoring_stats.get('system_metrics', {}).get('avg_cpu_percent', 0):.1f}%</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">메모리 사용률:</span>
                <span class="metric-value {'warning' if monitoring_stats.get('system_metrics', {}).get('avg_memory_percent', 0) > 80 else ''}">{monitoring_stats.get('system_metrics', {}).get('avg_memory_percent', 0):.1f}%</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">디스크 사용률:</span>
                <span class="metric-value {'warning' if monitoring_stats.get('system_metrics', {}).get('avg_disk_percent', 0) > 85 else ''}">{monitoring_stats.get('system_metrics', {}).get('avg_disk_percent', 0):.1f}%</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">활성 스레드:</span>
                <span class="metric-value">{monitoring_stats.get('system_metrics', {}).get('thread_count', 0)}개</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">가동 시간:</span>
                <span class="metric-value">{str(timedelta(seconds=int(monitoring_stats.get('uptime_seconds', 0))))}</span>
            </div>
        </div>
        
        <!-- 서비스 상태 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">🔧</span>
                서비스 상태
            </div>
            {''.join([f'''
            <div class="metric-item">
                <span class="metric-label">{service_name.replace('_', ' ').title()}:</span>
                <span class="metric-value {'warning' if service_info['status'] != 'healthy' else ''}">
                    <span class="status-indicator status-{'healthy' if service_info['status'] == 'healthy' else 'warning'}"></span>
                    {service_info['status'].upper()}
                </span>
            </div>
            ''' for service_name, service_info in monitoring_stats.get('service_status', {}).items()])}
        </div>
        
        <!-- 알림 설정 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">🔔</span>
                알림 설정
            </div>
            <div class="metric-item">
                <span class="metric-label">거래 실행 알림:</span>
                <span class="metric-value">{'✅ 활성' if config.ALERTS['trade_execution'] else '❌ 비활성'}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">포지션 업데이트:</span>
                <span class="metric-value">{'✅ 활성' if config.ALERTS['position_updates'] else '❌ 비활성'}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">리스크 경고:</span>
                <span class="metric-value">{'✅ 활성' if config.ALERTS['risk_warnings'] else '❌ 비활성'}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">시스템 오류:</span>
                <span class="metric-value">{'✅ 활성' if config.ALERTS['system_errors'] else '❌ 비활성'}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">성능 알림:</span>
                <span class="metric-value">{'✅ 활성' if config.ALERTS['performance_alerts'] else '❌ 비활성'}</span>
            </div>
        </div>
        
        <!-- NOTIFY 서비스 정보 -->
        <div class="card">
            <div class="card-title">
                <span class="card-icon">🏠</span>
                NOTIFY 서비스 정보
            </div>
            <div class="metric-item">
                <span class="metric-label">포트:</span>
                <span class="metric-value">8103</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">버전:</span>
                <span class="metric-value">V4 Ultimate</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">테마:</span>
                <span class="metric-value">{self.config_dash['theme'].upper()}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">WebSocket:</span>
                <span class="metric-value">{'✅ 활성' if self.config_dash['enable_websocket'] else '❌ 비활성'}</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">활성 연결:</span>
                <span class="metric-value" id="active-connections">{len(self.active_connections)}개</span>
            </div>
            <div class="metric-item">
                <span class="metric-label">차트 지원:</span>
                <span class="metric-value">{'✅ 활성' if CHART_AVAILABLE else '❌ 비활성 (matplotlib 필요)'}</span>
            </div>
        </div>
    </div>
    
    <div class="footer">
        <p><strong>🚀 Phoenix 95 V4 Ultimate - NOTIFY 서비스</strong></p>
        <p>📱 텔레그램 알림 | 📊 실시간 모니터링 | 🔔 통합 알림 관리</p>
        <p>포트 8103 | 개발: Phoenix 95 Team | 버전: V4-Ultimate</p>
        {f'<p>⚠️ 일부 기능 제한: matplotlib({str(CHART_AVAILABLE)}) | aioredis({str(REDIS_AVAILABLE)}) | asyncpg({str(POSTGRES_AVAILABLE)})</p>' if not all([CHART_AVAILABLE, REDIS_AVAILABLE, POSTGRES_AVAILABLE]) else ''}
    </div>
    
    <script>
        // 자동 새로고침
        let refreshInterval = {self.config_dash["refresh_interval"]} * 1000;
        let refreshTimer;
        
        function startAutoRefresh() {{
            refreshTimer = setInterval(() => {{
                location.reload();
            }}, refreshInterval);
        }}
        
        // 페이지 로드 시 초기화
        document.addEventListener('DOMContentLoaded', function() {{
            startAutoRefresh();
            console.log('🚀 Phoenix 95 V4 NOTIFY 대시보드 로드 완료');
        }});
        
        // 페이지 언로드 시 정리
        window.addEventListener('beforeunload', function() {{
            if (refreshTimer) {{
                clearInterval(refreshTimer);
            }}
        }});
    </script>
</body>
</html>
        """
    
    async def websocket_endpoint(self, websocket: WebSocket):
        """WebSocket 엔드포인트"""
        await websocket.accept()
        self.active_connections.append(websocket)
        
        try:
            while True:
                # 실시간 데이터 전송
                stats_data = {
                    "type": "stats_update",
                    "stats": {
                        "active_connections": len(self.active_connections),
                        "timestamp": time.time()
                    }
                }
                
                await websocket.send_text(json.dumps(stats_data))
                await asyncio.sleep(self.config_dash["refresh_interval"])
                
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
    
    async def broadcast_to_websockets(self, message: Dict):
        """모든 WebSocket 연결에 메시지 브로드캐스트"""
        if not self.active_connections:
            return
        
        disconnected = []
        message_text = json.dumps(message)
        
        for websocket in self.active_connections:
            try:
                await websocket.send_text(message_text)
            except:
                disconnected.append(websocket)
        
        # 연결 끊어진 소켓 제거
        for websocket in disconnected:
            self.active_connections.remove(websocket)

# ═══════════════════════════════════════════════════════════════════════════════
#                          🚀 NOTIFY 서비스 메인 애플리케이션
# ═══════════════════════════════════════════════════════════════════════════════

class NotifyServiceApp:
    """Phoenix 95 V4 NOTIFY 서비스 메인 애플리케이션"""
    
    def __init__(self):
        # FastAPI 앱 생성
        self.app = FastAPI(
            title="Phoenix 95 V4 Ultimate - NOTIFY Service",
            description="📱 알림 허브: 텔레그램 알림, 실시간 대시보드, 성능 모니터링",
            version="4.0.0-ultimate",
            docs_url="/docs",
            redoc_url="/redoc"
        )
        
        # CORS 미들웨어
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # 컴포넌트 초기화
        self.telegram_manager = TelegramNotificationManager()
        self.performance_monitor = PerformanceMonitor(self.telegram_manager)
        self.dashboard = RealTimeDashboard(self.telegram_manager, self.performance_monitor)
        
        # 백그라운드 태스크
        self.background_tasks = []
        
        # 서비스 통계
        self.service_stats = {
            "start_time": time.time(),
            "total_requests": 0,
            "total_notifications": 0,
            "total_alerts": 0,
            "uptime_seconds": 0
        }
        
        # 라우트 설정
        self._setup_routes()
        
        logging.info("🚀 NOTIFY 서비스 애플리케이션 초기화 완료")
    
    def _setup_routes(self):
        """API 라우트 설정"""
        
        @self.app.on_event("startup")
        async def startup_event():
            """서비스 시작 시 실행"""
            await self._startup_sequence()
        
        @self.app.on_event("shutdown")
        async def shutdown_event():
            """서비스 종료 시 실행"""
            await self._shutdown_sequence()
        
        @self.app.get("/", response_class=HTMLResponse)
        async def dashboard_home():
            """메인 대시보드"""
            try:
                return self.dashboard.generate_dashboard_html()
            except Exception as e:
                logging.error(f"🌐 대시보드 생성 실패: {e}")
                return HTMLResponse(f"<h1>대시보드 오류</h1><p>{str(e)}</p>", status_code=500)
        
        @self.app.get("/health")
        async def health_check():
            """헬스체크"""
            uptime = time.time() - self.service_stats["start_time"]
            
            return {
                "status": "healthy",
                "service": "NOTIFY",
                "port": config.DASHBOARD["port"],
                "version": "V4-Ultimate",
                "uptime_seconds": uptime,
                "uptime_formatted": str(timedelta(seconds=int(uptime))),
                "telegram_enabled": config.TELEGRAM["enabled"],
                "websocket_enabled": config.DASHBOARD["enable_websocket"],
                "chart_available": CHART_AVAILABLE,
                "redis_available": REDIS_AVAILABLE,
                "postgres_available": POSTGRES_AVAILABLE,
                "active_websocket_connections": len(self.dashboard.active_connections),
                "background_tasks": len(self.background_tasks),
                "timestamp": datetime.now().isoformat()
            }
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket 엔드포인트"""
            if config.DASHBOARD["enable_websocket"]:
                await self.dashboard.websocket_endpoint(websocket)
            else:
                await websocket.close(code=1003, reason="WebSocket disabled")
        
        @self.app.post("/api/notification/trade")
        async def send_trade_notification(trade_data: dict):
            """거래 알림 전송"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_notifications"] += 1
                
                await self.telegram_manager.send_trade_notification(trade_data)
                
                return {
                    "status": "success",
                    "message": "거래 알림 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 거래 알림 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/notification/position")
        async def send_position_update(position_data: dict):
            """포지션 업데이트 알림"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_notifications"] += 1
                
                await self.telegram_manager.send_position_update(position_data)
                
                return {
                    "status": "success",
                    "message": "포지션 알림 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 포지션 알림 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/notification/risk")
        async def send_risk_warning(risk_data: dict):
            """리스크 경고 알림"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_alerts"] += 1
                
                await self.telegram_manager.send_risk_warning(risk_data)
                
                return {
                    "status": "success",
                    "message": "리스크 경고 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 리스크 알림 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/notification/system")
        async def send_system_alert(alert_data: dict):
            """시스템 알림"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_alerts"] += 1
                
                await self.telegram_manager.send_system_alert(alert_data)
                
                return {
                    "status": "success",
                    "message": "시스템 알림 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 시스템 알림 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/notification/liquidation")
        async def send_liquidation_warning(liquidation_data: dict):
            """청산 위험 경고"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_alerts"] += 1
                
                await self.telegram_manager.send_liquidation_warning(liquidation_data)
                
                return {
                    "status": "success",
                    "message": "청산 경고 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 청산 경고 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/notification/daily-summary")
        async def send_daily_summary(summary_data: dict):
            """일일 요약 알림"""
            try:
                self.service_stats["total_requests"] += 1
                self.service_stats["total_notifications"] += 1
                
                await self.telegram_manager.send_daily_summary(summary_data)
                
                return {
                    "status": "success",
                    "message": "일일 요약 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📱 일일 요약 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/stats")
        async def get_service_stats():
            """서비스 통계 조회"""
            try:
                self.service_stats["uptime_seconds"] = time.time() - self.service_stats["start_time"]
                
                telegram_stats = self.telegram_manager.get_notification_stats()
                monitoring_stats = self.performance_monitor.get_monitoring_stats()
                
                return {
                    "service_stats": self.service_stats,
                    "telegram_stats": telegram_stats,
                    "monitoring_stats": monitoring_stats,
                    "dashboard_stats": {
                        "active_websocket_connections": len(self.dashboard.active_connections),
                        "chart_available": CHART_AVAILABLE,
                        "redis_available": REDIS_AVAILABLE,
                        "postgres_available": POSTGRES_AVAILABLE,
                        "websocket_enabled": config.DASHBOARD["enable_websocket"]
                    },
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"📊 통계 조회 API 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/api/config")
        async def get_service_config():
            """서비스 설정 조회"""
            return {
                "telegram_config": {
                    "enabled": config.TELEGRAM["enabled"],
                    "rate_limit": config.TELEGRAM["rate_limit"],
                    "batch_size": config.TELEGRAM["batch_size"],
                    "batch_interval": config.TELEGRAM["batch_interval"]
                },
                "dashboard_config": {
                    "port": config.DASHBOARD["port"],
                    "theme": config.DASHBOARD["theme"],
                    "refresh_interval": config.DASHBOARD["refresh_interval"],
                    "enable_charts": config.DASHBOARD["enable_charts"],
                    "enable_websocket": config.DASHBOARD["enable_websocket"]
                },
                "monitoring_config": {
                    "metrics_interval": config.MONITORING["metrics_interval"],
                    "alert_cooldown": config.MONITORING["alert_cooldown"],
                    "performance_thresholds": config.MONITORING["performance_thresholds"]
                },
                "alerts_config": config.ALERTS,
                "dependencies": {
                    "matplotlib": CHART_AVAILABLE,
                    "aioredis": REDIS_AVAILABLE,
                    "asyncpg": POSTGRES_AVAILABLE
                }
            }
        
        @self.app.post("/api/test/notification")
        async def test_notification():
            """테스트 알림 전송"""
            try:
                test_data = {
                    "service": "NOTIFY 테스트",
                    "status": "테스트 실행",
                    "message": "NOTIFY 서비스 테스트 알림입니다.",
                    "cpu_percent": 45.2,
                    "memory_percent": 62.8,
                    "active_positions": 3
                }
                
                await self.telegram_manager.send_system_alert(test_data, "LOW")
                
                return {
                    "status": "success",
                    "message": "테스트 알림 전송 완료",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"🧪 테스트 알림 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/admin/shutdown")
        async def admin_shutdown():
            """관리자 종료"""
            try:
                # 종료 알림 전송
                shutdown_data = {
                    "service": "NOTIFY 서비스",
                    "status": "종료",
                    "message": "관리자에 의해 NOTIFY 서비스가 종료됩니다.",
                    "cpu_percent": 0,
                    "memory_percent": 0,
                    "active_positions": 0
                }
                
                await self.telegram_manager.send_system_alert(shutdown_data, "HIGH")
                
                # 백그라운드에서 종료
                asyncio.create_task(self._delayed_shutdown())
                
                return {
                    "status": "success",
                    "message": "서비스 종료 시작",
                    "timestamp": datetime.now().isoformat()
                }
            except Exception as e:
                logging.error(f"🛑 관리자 종료 오류: {e}")
                raise HTTPException(status_code=500, detail=str(e))
    
    async def _startup_sequence(self):
        """시작 시퀀스"""
        try:
            logging.info("🚀 NOTIFY 서비스 시작 시퀀스 실행")
            
            # 백그라운드 태스크 시작
            telegram_tasks = await self.telegram_manager.start_message_processing()
            self.background_tasks.extend(telegram_tasks)
            
            monitoring_tasks = await self.performance_monitor.start_monitoring()
            self.background_tasks.extend(monitoring_tasks)
            
            logging.info(f"🔄 {len(self.background_tasks)}개 백그라운드 태스크 시작")
            
            # 시작 알림 전송
            startup_data = {
                "service": "NOTIFY 서비스",
                "status": "시작 완료",
                "message": f"Phoenix 95 V4 NOTIFY 서비스가 포트 {config.DASHBOARD['port']}에서 시작되었습니다.\n\n🔧 활성 기능:\n• 텔레그램 알림: {'✅' if config.TELEGRAM['enabled'] else '❌'}\n• 실시간 차트: {'✅' if CHART_AVAILABLE else '❌'}\n• Redis 연동: {'✅' if REDIS_AVAILABLE else '❌'}\n• PostgreSQL 연동: {'✅' if POSTGRES_AVAILABLE else '❌'}",
                "cpu_percent": psutil.cpu_percent(),
                "memory_percent": psutil.virtual_memory().percent,
                "active_positions": 0
            }
            
            await self.telegram_manager.send_system_alert(startup_data, "MEDIUM")
            
            logging.info("✅ NOTIFY 서비스 시작 시퀀스 완료")
            
        except Exception as e:
            logging.error(f"❌ NOTIFY 서비스 시작 실패: {e}")
            raise
    
    async def _shutdown_sequence(self):
        """종료 시퀀스"""
        try:
            logging.info("🛑 NOTIFY 서비스 종료 시퀀스 실행")
            
            # 종료 알림 전송
            shutdown_data = {
                "service": "NOTIFY 서비스",
                "status": "정상 종료",
                "message": "Phoenix 95 V4 NOTIFY 서비스가 안전하게 종료됩니다.",
                "cpu_percent": psutil.cpu_percent(),
                "memory_percent": psutil.virtual_memory().percent,
                "active_positions": 0
            }
            
            await self.telegram_manager.send_system_alert(shutdown_data, "MEDIUM")
            
            # WebSocket 연결 종료
            for websocket in self.dashboard.active_connections:
                try:
                    await websocket.close()
                except:
                    pass
            
            # 백그라운드 태스크 종료
            for task in self.background_tasks:
                if not task.done():
                    task.cancel()
            
            # 남은 메시지 처리 (5초 대기)
            await asyncio.sleep(5)
            
            logging.info("✅ NOTIFY 서비스 종료 시퀀스 완료")
            
        except Exception as e:
            logging.error(f"❌ NOTIFY 서비스 종료 오류: {e}")
    
    async def _delayed_shutdown(self):
        """지연된 종료 (관리자 명령)"""
        await asyncio.sleep(3)  # 응답 전송 대기
        os._exit(0)  # 강제 종료

# ═══════════════════════════════════════════════════════════════════════════════
#                              🚀 메인 실행부
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    """메인 실행 함수"""
    try:
        # 로깅 설정
        log_path = Path(config.DATA_STORAGE["log_path"])
        log_path.mkdir(parents=True, exist_ok=True)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s | %(name)s | %(levelname)s | [NOTIFY-8103] %(message)s',
            handlers=[
                logging.FileHandler(log_path / 'notify_service.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        # 서비스 애플리케이션 생성
        notify_app = NotifyServiceApp()
        
        # 서버 시작 메시지
        logging.info("=" * 80)
        logging.info("🚀 Phoenix 95 V4 Ultimate - NOTIFY 서비스 시작")
        logging.info("=" * 80)
        logging.info(f"📱 포트: {config.DASHBOARD['port']}")
        logging.info(f"📊 대시보드: http://localhost:{config.DASHBOARD['port']}")
        logging.info(f"🔔 텔레그램 알림: {'✅ 활성' if config.TELEGRAM['enabled'] else '❌ 비활성'}")
        logging.info(f"🌐 WebSocket: {'✅ 활성' if config.DASHBOARD['enable_websocket'] else '❌ 비활성'}")
        logging.info(f"📈 실시간 차트: {'✅ 활성' if CHART_AVAILABLE else '❌ 비활성 (matplotlib 필요)'}")
        logging.info(f"🎨 테마: {config.DASHBOARD['theme'].upper()}")
        logging.info(f"💾 Redis: {'✅ 활성' if REDIS_AVAILABLE else '❌ 비활성'}")
        logging.info(f"🗄️ PostgreSQL: {'✅ 활성' if POSTGRES_AVAILABLE else '❌ 비활성'}")
        logging.info("=" * 80)
        
        # 의존성 경고
        if not CHART_AVAILABLE:
            logging.warning("⚠️ matplotlib 미설치 - 차트 기능 비활성화")
        if not REDIS_AVAILABLE:
            logging.warning("⚠️ aioredis 미설치 - Redis 기능 비활성화")
        if not POSTGRES_AVAILABLE:
            logging.warning("⚠️ asyncpg 미설치 - PostgreSQL 기능 비활성화")
        
        # 서버 실행
        uvicorn_config = uvicorn.Config(
            notify_app.app,
            host=config.DASHBOARD["host"],
            port=config.DASHBOARD["port"],
            log_level="info",
            access_log=True,
            reload=False,
            workers=1
        )
        
        server = uvicorn.Server(uvicorn_config)
        await server.serve()
        
    except KeyboardInterrupt:
        logging.info("🛑 사용자에 의한 서비스 종료")
    except Exception as e:
        logging.error(f"❌ NOTIFY 서비스 실행 오류: {e}")
        logging.error(traceback.format_exc())
    finally:
        logging.info("👋 Phoenix 95 V4 NOTIFY 서비스 종료")

if __name__ == "__main__":
    asyncio.run(main())