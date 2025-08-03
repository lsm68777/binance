#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚡ Phoenix 95 V4 EXECUTE 서비스 - 거래 실행 엔진 (수정된 완전판)
================================================================================

🎯 핵심 기능:
- 20x 레버리지 거래 실행 (이솔레이티드 마진)
- 2% 익절/손절 자동 설정
- 실시간 포지션 모니터링 
- Kelly Criterion 포지션 사이징
- 3단계 리스크 검증 시스템
- 바이낸스 API 연동

🔥 포트: 8102
🏛️ 등급: 헤지펀드급 거래 실행 시스템

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
import numpy as np
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from collections import deque
from decimal import Decimal
import traceback
import hashlib
import hmac

# FastAPI 및 관련 라이브러리
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | Phoenix95-Execute | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('logs/execute_service.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#                              🔧 V4 시스템 설정
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Phoenix95ExecuteConfig:
    """Phoenix 95 V4 거래 실행 설정"""
    
    # 🏛️ 헤지펀드급 거래 설정
    LEVERAGE = 20                           # 20배 레버리지 고정
    MARGIN_MODE = "ISOLATED"                # 이솔레이티드 마진
    STOP_LOSS_PERCENT = 0.02               # 2% 손절
    TAKE_PROFIT_PERCENT = 0.02             # 2% 익절
    MAX_POSITION_SIZE = 0.05               # 최대 포지션 크기 5%
    MIN_CONFIDENCE = 0.75                  # 최소 신뢰도 75%
    
    # 🛡️ 리스크 관리
    MAX_DAILY_LOSS = 0.05                  # 일일 최대 손실 5%
    MAX_POSITIONS = 5                      # 최대 동시 포지션 5개
    POSITION_TIMEOUT = 86400               # 24시간 후 자동 청산
    LIQUIDATION_BUFFER = 0.1               # 청산 방지 버퍼 10%
    
    # 💰 Kelly Criterion 설정
    KELLY_MAX_FRACTION = 0.25              # Kelly 최대 비율 25%
    WIN_RATE_ADJUSTMENT = 0.85             # 승률 조정 계수
    
    # 🚀 성능 설정
    ORDER_TIMEOUT = 30                     # 주문 타임아웃 30초
    PRICE_CHECK_INTERVAL = 1               # 1초마다 가격 체크
    MONITOR_INTERVAL = 5                   # 5초마다 포지션 모니터링
    
    # 📱 텔레그램 알림
    TELEGRAM_TOKEN = "7386542811:AAEZ21p30rES1k8NxNM2xbZ53U44PI9D5CY"
    TELEGRAM_CHAT_ID = "7590895952"
    
    # 🏦 허용된 심볼
    ALLOWED_SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT",
        "XRPUSDT", "SOLUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
        "MATICUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT"
    ]

# 전역 설정 인스턴스
config = Phoenix95ExecuteConfig()

# ═══════════════════════════════════════════════════════════════════════════════
#                              📊 데이터 모델
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradingSignal:
    """거래 신호 모델"""
    signal_id: str
    symbol: str
    action: str  # buy/sell/long/short
    price: float
    confidence: float
    phoenix95_score: Optional[float] = None
    kelly_ratio: Optional[float] = None
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

@dataclass
class Position:
    """포지션 모델"""
    position_id: str
    signal_id: str
    symbol: str
    action: str
    
    # 거래 정보
    entry_price: float
    quantity: float
    leverage: int
    margin_required: float
    
    # 가격 정보
    stop_loss_price: float
    take_profit_price: float
    liquidation_price: float
    current_price: float = 0.0
    
    # P&L 정보
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    pnl_percentage: float = 0.0
    roe: float = 0.0
    
    # 상태
    status: str = "ACTIVE"  # ACTIVE/CLOSED/LIQUIDATED
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: Optional[float] = None
    
    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.updated_at == 0.0:
            self.updated_at = time.time()

@dataclass
class ExecutionResult:
    """실행 결과 모델"""
    execution_id: str
    status: str  # SUCCESS/FAILED/BLOCKED
    message: str
    position: Optional[Position] = None
    error_details: Optional[Dict] = None
    execution_time_ms: float = 0.0
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

# ═══════════════════════════════════════════════════════════════════════════════
#                              ⚡ 핵심 거래 실행 엔진
# ═══════════════════════════════════════════════════════════════════════════════

class Phoenix95ExecuteEngine:
    """Phoenix 95 V4 거래 실행 엔진"""
    
    def __init__(self):
        self.active_positions: Dict[str, Position] = {}
        self.position_history: List[Position] = []
        self.daily_pnl = 0.0
        self.total_trades = 0
        self.successful_trades = 0
        
        # 성능 메트릭
        self.execution_stats = {
            "total_executions": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "avg_execution_time": 0.0,
            "total_volume": 0.0,
            "current_positions": 0,
            "daily_pnl": 0.0,
            "win_rate": 0.0
        }
        
        # 리스크 추적
        self.risk_metrics = {
            "max_drawdown": 0.0,
            "current_exposure": 0.0,
            "margin_utilization": 0.0,
            "liquidation_warnings": 0
        }
        
        logger.info("⚡ Phoenix 95 V4 거래 실행 엔진 초기화 완료")
        logger.info(f"   설정: {config.LEVERAGE}x 레버리지, {config.MARGIN_MODE} 마진")
        logger.info(f"   익절/손절: ±{config.TAKE_PROFIT_PERCENT*100:.0f}%")
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          🎯 메인 거래 실행 함수
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def execute_trade_complete(self, signal: TradingSignal) -> ExecutionResult:
        """완전한 거래 실행 프로세스"""
        execution_start = time.time()
        execution_id = f"EXEC_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
        
        try:
            logger.info(f"🚀 거래 실행 시작: {execution_id}")
            logger.info(f"   신호: {signal.symbol} {signal.action} ${signal.price:,.2f}")
            logger.info(f"   신뢰도: {signal.confidence:.1%}")
            
            # 1단계: 기본 검증
            basic_check = await self._basic_validation(signal)
            if not basic_check["valid"]:
                return self._create_failed_result(
                    execution_id, f"기본 검증 실패: {basic_check['reason']}", 
                    execution_start
                )
            
            # 2단계: 리스크 검증 (3단계)
            risk_check = await self._comprehensive_risk_check(signal)
            if not risk_check["approved"]:
                return self._create_failed_result(
                    execution_id, f"리스크 검증 실패: {risk_check['reason']}", 
                    execution_start
                )
            
            # 3단계: Kelly Criterion 포지션 사이징
            position_size = await self._calculate_kelly_position_size(signal)
            if position_size <= 0:
                return self._create_failed_result(
                    execution_id, "포지션 크기 계산 실패", execution_start
                )
            
            # 4단계: 레버리지 계산 및 마진 요구사항
            leverage_info = await self._calculate_leverage_requirements(
                signal, position_size
            )
            
            # 5단계: 바이낸스 거래 실행 (시뮬레이션)
            order_result = await self._execute_binance_order(
                signal, leverage_info
            )
            
            if not order_result["success"]:
                return self._create_failed_result(
                    execution_id, f"주문 실행 실패: {order_result['error']}", 
                    execution_start
                )
            
            # 6단계: 포지션 생성 및 추적 시작
            position = await self._create_position(
                execution_id, signal, leverage_info, order_result
            )
            
            # 7단계: 실시간 모니터링 시작
            asyncio.create_task(self._monitor_position(position))
            
            # 8단계: 텔레그램 알림 전송
            await self._send_execution_notification(position)
            
            # 통계 업데이트
            execution_time = (time.time() - execution_start) * 1000
            self._update_execution_stats(True, execution_time)
            
            logger.info(f"✅ 거래 실행 성공: {execution_id}")
            logger.info(f"   포지션 ID: {position.position_id}")
            logger.info(f"   실행 시간: {execution_time:.1f}ms")
            
            return ExecutionResult(
                execution_id=execution_id,
                status="SUCCESS",
                message="거래 실행 성공",
                position=position,
                execution_time_ms=execution_time
            )
            
        except Exception as e:
            logger.error(f"❌ 거래 실행 중 오류: {e}")
            logger.error(f"   상세: {traceback.format_exc()}")
            
            execution_time = (time.time() - execution_start) * 1000
            self._update_execution_stats(False, execution_time)
            
            return self._create_failed_result(
                execution_id, f"시스템 오류: {str(e)}", execution_start
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          🔍 검증 및 리스크 관리 함수들
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _basic_validation(self, signal: TradingSignal) -> Dict:
        """기본 검증"""
        try:
            # 심볼 검증
            if signal.symbol not in config.ALLOWED_SYMBOLS:
                return {"valid": False, "reason": f"허용되지 않은 심볼: {signal.symbol}"}
            
            # 신뢰도 검증
            if signal.confidence < config.MIN_CONFIDENCE:
                return {"valid": False, "reason": f"신뢰도 부족: {signal.confidence:.1%}"}
            
            # 액션 검증
            if signal.action.lower() not in ["buy", "sell", "long", "short"]:
                return {"valid": False, "reason": f"잘못된 액션: {signal.action}"}
            
            # 가격 검증
            if signal.price <= 0:
                return {"valid": False, "reason": f"잘못된 가격: {signal.price}"}
            
            return {"valid": True, "reason": "기본 검증 통과"}
            
        except Exception as e:
            return {"valid": False, "reason": f"검증 중 오류: {e}"}
    
    async def _comprehensive_risk_check(self, signal: TradingSignal) -> Dict:
        """3단계 종합 리스크 검증"""
        try:
            # Level 1: 포지션 수 검증
            if len(self.active_positions) >= config.MAX_POSITIONS:
                return {
                    "approved": False, 
                    "level": 1,
                    "reason": f"최대 포지션 수 초과: {len(self.active_positions)}/{config.MAX_POSITIONS}"
                }
            
            # Level 2: 일일 손실 한도 검증
            if abs(self.daily_pnl) >= config.MAX_DAILY_LOSS:
                return {
                    "approved": False,
                    "level": 2, 
                    "reason": f"일일 손실 한도 초과: {self.daily_pnl:.1%}"
                }
            
            # Level 3: 심볼 중복 검증
            symbol_positions = [p for p in self.active_positions.values() 
                              if p.symbol == signal.symbol]
            if len(symbol_positions) >= 2:
                return {
                    "approved": False,
                    "level": 3,
                    "reason": f"{signal.symbol} 포지션 중복: {len(symbol_positions)}개"
                }
            
            # Level 4: 시장 시간 검증 (선택적)
            current_hour = datetime.now().hour
            if current_hour < 6 or current_hour > 22:  # 6시-22시 외
                logger.warning(f"⚠️ 비활성 시간대 거래: {current_hour}시")
            
            return {
                "approved": True,
                "level": 0,
                "reason": "모든 리스크 검증 통과"
            }
            
        except Exception as e:
            return {
                "approved": False, 
                "level": -1,
                "reason": f"리스크 검증 중 오류: {e}"
            }
    
    async def _calculate_kelly_position_size(self, signal: TradingSignal) -> float:
        """Kelly Criterion 포지션 사이징"""
        try:
            # 기본 포트폴리오 가정
            portfolio_value = 10000  # $10,000
            
            # Kelly 공식 입력값
            win_rate = signal.confidence * config.WIN_RATE_ADJUSTMENT
            avg_win = 1 + config.TAKE_PROFIT_PERCENT  # 102%
            avg_loss = 1 - config.STOP_LOSS_PERCENT   # 98%
            
            # Kelly 공식: f* = (bp - q) / b
            # b = 평균 이익/손실 비율, p = 승률, q = 패율
            win_loss_ratio = (avg_win - 1) / (1 - avg_loss)  # 약 1:1
            kelly_fraction = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio
            
            # 안전 한계 적용
            kelly_fraction = max(0.01, min(kelly_fraction, config.KELLY_MAX_FRACTION))
            
            # Phoenix 95 점수 보정
            if signal.phoenix95_score:
                phoenix_multiplier = min(1.2, signal.phoenix95_score + 0.5)
                kelly_fraction *= phoenix_multiplier
            
            # 최종 포지션 크기
            position_size = portfolio_value * kelly_fraction
            position_size = min(position_size, portfolio_value * config.MAX_POSITION_SIZE)
            
            logger.info(f"📊 Kelly 포지션 사이징 완료:")
            logger.info(f"   승률: {win_rate:.1%}")
            logger.info(f"   Kelly 비율: {kelly_fraction:.1%}")
            logger.info(f"   포지션 크기: ${position_size:,.2f}")
            
            return position_size
            
        except Exception as e:
            logger.error(f"❌ Kelly 계산 실패: {e}")
            return 0.0
    
    async def _calculate_leverage_requirements(self, signal: TradingSignal, 
                                             position_size: float) -> Dict:
        """레버리지 요구사항 계산"""
        try:
            leverage = config.LEVERAGE
            price = signal.price
            action = signal.action.lower()
            
            # 실제 포지션 크기 (레버리지 적용)
            actual_position_size = position_size * leverage
            
            # 필요 마진
            margin_required = actual_position_size / leverage
            
            # 수량 계산
            quantity = actual_position_size / price
            
            # 청산가 계산
            liquidation_price = self._calculate_liquidation_price(
                price, action, leverage
            )
            
            # 손절/익절가 계산
            if action in ["buy", "long"]:
                stop_loss_price = price * (1 - config.STOP_LOSS_PERCENT)
                take_profit_price = price * (1 + config.TAKE_PROFIT_PERCENT)
            else:  # sell, short
                stop_loss_price = price * (1 + config.STOP_LOSS_PERCENT)
                take_profit_price = price * (1 - config.TAKE_PROFIT_PERCENT)
            
            return {
                "leverage": leverage,
                "position_size": position_size,
                "actual_position_size": actual_position_size,
                "margin_required": margin_required,
                "quantity": quantity,
                "liquidation_price": liquidation_price,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "margin_ratio": margin_required / 10000  # 포트폴리오 대비
            }
            
        except Exception as e:
            logger.error(f"❌ 레버리지 계산 실패: {e}")
            return {}
    
    def _calculate_liquidation_price(self, entry_price: float, action: str, 
                                   leverage: int) -> float:
        """청산가 계산"""
        try:
            maintenance_margin = 0.004  # 0.4% 유지 마진
            
            if action in ["buy", "long"]:
                # 롱 포지션 청산가
                liquidation_price = entry_price * (
                    1 - (1/leverage - maintenance_margin - config.LIQUIDATION_BUFFER)
                )
            else:
                # 숏 포지션 청산가
                liquidation_price = entry_price * (
                    1 + (1/leverage - maintenance_margin - config.LIQUIDATION_BUFFER)
                )
            
            return max(0, liquidation_price)
            
        except Exception as e:
            logger.error(f"❌ 청산가 계산 실패: {e}")
            return 0.0
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          💰 거래 실행 및 포지션 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _execute_binance_order(self, signal: TradingSignal, 
                                   leverage_info: Dict) -> Dict:
        """바이낸스 주문 실행 (시뮬레이션)"""
        try:
            # 실제 환경에서는 ccxt 라이브러리 사용
            # 여기서는 시뮬레이션으로 처리
            
            # 주문 실행 지연 시뮬레이션
            await asyncio.sleep(0.1)
            
            # 슬리피지 시뮬레이션
            slippage = np.random.uniform(0.0001, 0.001)  # 0.01%-0.1%
            if signal.action.lower() in ["buy", "long"]:
                execution_price = signal.price * (1 + slippage)
            else:
                execution_price = signal.price * (1 - slippage)
            
            # 성공률 시뮬레이션 (95%)
            success = np.random.random() < 0.95
            
            if success:
                order_id = f"ORD_{int(time.time()*1000)}"
                
                logger.info(f"💰 바이낸스 주문 실행:")
                logger.info(f"   주문 ID: {order_id}")
                logger.info(f"   실행가: ${execution_price:,.2f}")
                logger.info(f"   슬리피지: {slippage*100:.3f}%")
                logger.info(f"   수량: {leverage_info['quantity']:.6f}")
                
                return {
                    "success": True,
                    "order_id": order_id,
                    "execution_price": execution_price,
                    "slippage": slippage,
                    "timestamp": time.time()
                }
            else:
                return {
                    "success": False,
                    "error": "시장 조건으로 인한 주문 실패"
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"주문 실행 중 오류: {e}"
            }
    
    async def _create_position(self, execution_id: str, signal: TradingSignal, 
                             leverage_info: Dict, order_result: Dict) -> Position:
        """포지션 생성"""
        try:
            position_id = f"POS_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
            
            position = Position(
                position_id=position_id,
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                action=signal.action,
                entry_price=order_result["execution_price"],
                quantity=leverage_info["quantity"],
                leverage=leverage_info["leverage"],
                margin_required=leverage_info["margin_required"],
                stop_loss_price=leverage_info["stop_loss_price"],
                take_profit_price=leverage_info["take_profit_price"],
                liquidation_price=leverage_info["liquidation_price"],
                current_price=order_result["execution_price"]
            )
            
            # 활성 포지션에 추가
            self.active_positions[position_id] = position
            self.execution_stats["current_positions"] = len(self.active_positions)
            
            logger.info(f"📍 포지션 생성 완료:")
            logger.info(f"   포지션 ID: {position_id}")
            logger.info(f"   심볼: {position.symbol}")
            logger.info(f"   레버리지: {position.leverage}x")
            logger.info(f"   마진: ${position.margin_required:,.2f}")
            logger.info(f"   손절가: ${position.stop_loss_price:,.2f}")
            logger.info(f"   익절가: ${position.take_profit_price:,.2f}")
            logger.info(f"   청산가: ${position.liquidation_price:,.2f}")
            
            return position
            
        except Exception as e:
            logger.error(f"❌ 포지션 생성 실패: {e}")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          📊 실시간 포지션 모니터링
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _monitor_position(self, position: Position):
        """실시간 포지션 모니터링"""
        logger.info(f"👀 포지션 모니터링 시작: {position.position_id}")
        
        try:
            start_time = time.time()
            
            while position.status == "ACTIVE":
                # 현재가 조회 (시뮬레이션)
                current_price = await self._get_current_price(position.symbol)
                position.current_price = current_price
                position.updated_at = time.time()
                
                # P&L 계산
                pnl_info = self._calculate_pnl(position, current_price)
                position.unrealized_pnl = pnl_info["unrealized_pnl"]
                position.pnl_percentage = pnl_info["pnl_percentage"]
                position.roe = pnl_info["roe"]
                
                # 청산 조건 체크
                close_reason = self._check_close_conditions(position, current_price)
                if close_reason:
                    await self._close_position(position, current_price, close_reason)
                    break
                
                # 청산 위험 알림
                liquidation_distance = abs(current_price - position.liquidation_price) / current_price
                if liquidation_distance < 0.1:  # 10% 이내 접근
                    await self._send_liquidation_warning(position)
                    self.risk_metrics["liquidation_warnings"] += 1
                
                # 시간 제한 체크 (24시간)
                if time.time() - start_time > config.POSITION_TIMEOUT:
                    await self._close_position(position, current_price, "시간 만료")
                    break
                
                # 모니터링 간격
                await asyncio.sleep(config.MONITOR_INTERVAL)
                
        except Exception as e:
            logger.error(f"❌ 포지션 모니터링 오류 ({position.position_id}): {e}")
            # 오류 시 포지션 정리
            if position.position_id in self.active_positions:
                await self._close_position(position, position.current_price, "모니터링 오류")
    
    async def _get_current_price(self, symbol: str) -> float:
        """현재가 조회 (시뮬레이션)"""
        try:
            # 실제로는 바이낸스 API 호출
            # 여기서는 랜덤 가격 변동 시뮬레이션
            base_prices = {
                "BTCUSDT": 45000, "ETHUSDT": 3000, "BNBUSDT": 320,
                "ADAUSDT": 0.45, "DOGEUSDT": 0.08, "XRPUSDT": 0.55
            }
            
            base_price = base_prices.get(symbol, 1000)
            # ±2% 범위에서 가격 변동
            change = np.random.uniform(-0.02, 0.02)
            return base_price * (1 + change)
            
        except Exception as e:
            logger.warning(f"⚠️ 가격 조회 실패 ({symbol}): {e}")
            return 0.0
    
    def _calculate_pnl(self, position: Position, current_price: float) -> Dict:
        """P&L 계산"""
        try:
            if position.action.lower() in ["buy", "long"]:
                price_diff = current_price - position.entry_price
            else:  # sell, short
                price_diff = position.entry_price - current_price
            
            # 미실현 손익 (레버리지 적용)
            unrealized_pnl = price_diff * position.quantity
            
            # 퍼센트 수익률 (마진 기준)
            pnl_percentage = unrealized_pnl / position.margin_required * 100
            
            # ROE (Return on Equity) - 레버리지 수익률
            roe = (price_diff / position.entry_price) * position.leverage * 100
            
            return {
                "unrealized_pnl": unrealized_pnl,
                "pnl_percentage": pnl_percentage,
                "roe": roe
            }
            
        except Exception as e:
            logger.error(f"❌ P&L 계산 실패: {e}")
            return {"unrealized_pnl": 0.0, "pnl_percentage": 0.0, "roe": 0.0}
    
    def _check_close_conditions(self, position: Position, current_price: float) -> Optional[str]:
        """포지션 청산 조건 체크"""
        try:
            # 손절 조건
            if position.action.lower() in ["buy", "long"]:
                if current_price <= position.stop_loss_price:
                    return "손절 실행"
                if current_price >= position.take_profit_price:
                    return "익절 실행"
            else:  # sell, short
                if current_price >= position.stop_loss_price:
                    return "손절 실행"
                if current_price <= position.take_profit_price:
                    return "익절 실행"
            
            # 청산 조건 (청산가 근접)
            liquidation_threshold = 0.02  # 2% 이내
            if position.action.lower() in ["buy", "long"]:
                if current_price <= position.liquidation_price * (1 + liquidation_threshold):
                    return "강제 청산 (롱)"
            else:
                if current_price >= position.liquidation_price * (1 - liquidation_threshold):
                    return "강제 청산 (숏)"
            
            return None
            
        except Exception as e:
            logger.error(f"❌ 청산 조건 체크 실패: {e}")
            return "시스템 오류"
    
    async def _close_position(self, position: Position, close_price: float, reason: str):
        """포지션 청산"""
        try:
            logger.info(f"🔥 포지션 청산 시작: {position.position_id}")
            logger.info(f"   청산 사유: {reason}")
            logger.info(f"   청산가: ${close_price:,.2f}")
            
            # 최종 P&L 계산
            final_pnl = self._calculate_pnl(position, close_price)
            position.realized_pnl = final_pnl["unrealized_pnl"]
            position.pnl_percentage = final_pnl["pnl_percentage"]
            position.roe = final_pnl["roe"]
            
            # 포지션 상태 업데이트
            position.status = "CLOSED"
            position.current_price = close_price
            position.closed_at = time.time()
            
            # 일일 P&L 업데이트
            self.daily_pnl += position.realized_pnl
            
            # 통계 업데이트
            self.total_trades += 1
            if position.realized_pnl > 0:
                self.successful_trades += 1
            
            # 활성 포지션에서 제거
            if position.position_id in self.active_positions:
                del self.active_positions[position.position_id]
            
            # 히스토리에 추가
            self.position_history.append(position)
            
            # 통계 업데이트
            self.execution_stats["current_positions"] = len(self.active_positions)
            self.execution_stats["daily_pnl"] = self.daily_pnl
            self.execution_stats["win_rate"] = (
                self.successful_trades / self.total_trades * 100 
                if self.total_trades > 0 else 0
            )
            
            # 청산 알림 전송
            await self._send_close_notification(position, reason)
            
            logger.info(f"✅ 포지션 청산 완료:")
            logger.info(f"   실현 손익: ${position.realized_pnl:+,.2f}")
            logger.info(f"   ROE: {position.roe:+.2f}%")
            logger.info(f"   일일 P&L: ${self.daily_pnl:+,.2f}")
            
        except Exception as e:
            logger.error(f"❌ 포지션 청산 실패: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          📱 알림 시스템
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _send_execution_notification(self, position: Position):
        """거래 실행 알림"""
        try:
            message = (
                f"🚀 <b>거래 실행 완료</b>\n\n"
                f"🪙 심볼: <code>{position.symbol}</code>\n"
                f"📊 액션: <b>{position.action.upper()}</b>\n"
                f"💰 진입가: <code>${position.entry_price:,.2f}</code>\n"
                f"⚡ 레버리지: <b>{position.leverage}x</b> (ISOLATED)\n"
                f"💸 마진: <code>${position.margin_required:,.2f}</code>\n"
                f"📈 익절가: <code>${position.take_profit_price:,.2f}</code> (+2%)\n"
                f"📉 손절가: <code>${position.stop_loss_price:,.2f}</code> (-2%)\n"
                f"⚠️ 청산가: <code>${position.liquidation_price:,.2f}</code>\n"
                f"🆔 포지션: <code>{position.position_id}</code>\n\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            
            await self._send_telegram_message(message)
            
        except Exception as e:
            logger.warning(f"⚠️ 실행 알림 전송 실패: {e}")
    
    async def _send_close_notification(self, position: Position, reason: str):
        """포지션 청산 알림"""
        try:
            pnl_emoji = "📈" if position.realized_pnl > 0 else "📉"
            reason_emoji = "🎯" if "익절" in reason else "🛡️" if "손절" in reason else "⚠️"
            
            message = (
                f"{pnl_emoji} <b>포지션 청산</b> {reason_emoji}\n\n"
                f"🪙 심볼: <code>{position.symbol}</code>\n"
                f"📊 액션: <b>{position.action.upper()}</b>\n"
                f"💰 진입가: <code>${position.entry_price:,.2f}</code>\n"
                f"💸 청산가: <code>${position.current_price:,.2f}</code>\n"
                f"⚡ 레버리지: <b>{position.leverage}x</b>\n"
                f"💵 실현손익: <b>${position.realized_pnl:+,.2f}</b>\n"
                f"📊 ROE: <b>{position.roe:+.2f}%</b>\n"
                f"🔥 사유: <i>{reason}</i>\n"
                f"🆔 포지션: <code>{position.position_id}</code>\n\n"
                f"📈 일일 P&L: <b>${self.daily_pnl:+,.2f}</b>\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            
            await self._send_telegram_message(message)
            
        except Exception as e:
            logger.warning(f"⚠️ 청산 알림 전송 실패: {e}")
    
    async def _send_liquidation_warning(self, position: Position):
        """청산 위험 경고"""
        try:
            distance = abs(position.current_price - position.liquidation_price) / position.current_price * 100
            
            message = (
                f"🆘 <b>청산 위험 경고</b>\n\n"
                f"🪙 심볼: <code>{position.symbol}</code>\n"
                f"📊 포지션: <b>{position.action.upper()}</b> {position.leverage}x\n"
                f"💰 현재가: <code>${position.current_price:,.2f}</code>\n"
                f"⚠️ 청산가: <code>${position.liquidation_price:,.2f}</code>\n"
                f"📏 거리: <b>{distance:.2f}%</b>\n"
                f"💔 미실현: <b>${position.unrealized_pnl:+,.2f}</b>\n"
                f"🆔 포지션: <code>{position.position_id}</code>\n\n"
                f"⚡ 즉시 확인 필요!"
            )
            
            await self._send_telegram_message(message)
            
        except Exception as e:
            logger.warning(f"⚠️ 청산 경고 전송 실패: {e}")
    
    async def _send_telegram_message(self, message: str):
        """텔레그램 메시지 전송"""
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
            data = {
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info("📱 텔레그램 알림 전송 성공")
                    else:
                        logger.warning(f"📱 텔레그램 전송 실패: {response.status}")
                        
        except Exception as e:
            logger.warning(f"📱 텔레그램 오류: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    #                          📊 유틸리티 함수들
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _create_failed_result(self, execution_id: str, reason: str, 
                            start_time: float) -> ExecutionResult:
        """실패 결과 생성"""
        execution_time = (time.time() - start_time) * 1000
        self._update_execution_stats(False, execution_time)
        
        return ExecutionResult(
            execution_id=execution_id,
            status="FAILED",
            message=reason,
            execution_time_ms=execution_time
        )
    
    def _update_execution_stats(self, success: bool, execution_time: float):
        """실행 통계 업데이트"""
        self.execution_stats["total_executions"] += 1
        
        if success:
            self.execution_stats["successful_executions"] += 1
        else:
            self.execution_stats["failed_executions"] += 1
        
        # 평균 실행 시간 업데이트
        total = self.execution_stats["total_executions"]
        current_avg = self.execution_stats["avg_execution_time"]
        self.execution_stats["avg_execution_time"] = (
            (current_avg * (total - 1) + execution_time) / total
        )
    
    def get_system_status(self) -> Dict:
        """시스템 상태 조회"""
        return {
            "service": "Phoenix95-Execute-V4",
            "status": "ACTIVE",
            "port": 8102,
            "active_positions": len(self.active_positions),
            "daily_pnl": self.daily_pnl,
            "execution_stats": self.execution_stats,
            "risk_metrics": self.risk_metrics,
            "config": {
                "leverage": config.LEVERAGE,
                "margin_mode": config.MARGIN_MODE,
                "stop_loss": f"{config.STOP_LOSS_PERCENT*100:.0f}%",
                "take_profit": f"{config.TAKE_PROFIT_PERCENT*100:.0f}%",
                "max_positions": config.MAX_POSITIONS
            }
        }

# ═══════════════════════════════════════════════════════════════════════════════
#                              🌐 FastAPI 웹 서비스
# ═══════════════════════════════════════════════════════════════════════════════

# FastAPI 앱 생성
app = FastAPI(
    title="Phoenix 95 V4 Execute Service",
    description="헤지펀드급 20x 레버리지 거래 실행 시스템",
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS 미들웨어
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 거래 실행 엔진 인스턴스
execute_engine = Phoenix95ExecuteEngine()

# 요청 모델
class ExecuteRequest(BaseModel):
    signal_id: str
    symbol: str
    action: str
    price: float
    confidence: float
    phoenix95_score: Optional[float] = None
    kelly_ratio: Optional[float] = None

# ═══════════════════════════════════════════════════════════════════════════════
#                              🛣️ API 라우트
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """메인 대시보드"""
    
    # 활성 포지션 테이블 HTML 생성
    positions_html = ""
    if execute_engine.active_positions:
        positions_rows = []
        for pos in execute_engine.active_positions.values():
            pnl_class = "profit" if pos.unrealized_pnl >= 0 else "loss"
            roe_class = "profit" if pos.roe >= 0 else "loss"
            
            row = f"""
                        <tr>
                            <td>{pos.symbol}</td>
                            <td>{pos.action.upper()}</td>
                            <td>{pos.leverage}x</td>
                            <td>${pos.entry_price:,.2f}</td>
                            <td>${pos.current_price:,.2f}</td>
                            <td class="{pnl_class}">${pos.unrealized_pnl:+,.2f}</td>
                            <td class="{roe_class}">{pos.roe:+.2f}%</td>
                        </tr>"""
            positions_rows.append(row)
        
        positions_html = f"""
            <div class="status-card">
                <div class="status-title">📊 활성 포지션 ({len(execute_engine.active_positions)}개)</div>
                <table class="positions-table">
                    <thead>
                        <tr>
                            <th>심볼</th>
                            <th>방향</th>
                            <th>레버리지</th>
                            <th>진입가</th>
                            <th>현재가</th>
                            <th>P&L</th>
                            <th>ROE</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(positions_rows)}
                    </tbody>
                </table>
            </div>"""
    
    # 일일 P&L 색상 결정
    pnl_color = "#4caf50" if execute_engine.daily_pnl >= 0 else "#f44336"
    
    # CSS와 JavaScript를 분리해서 올바른 문법 사용
    css_styles = """
        body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 20px; 
               background: linear-gradient(135deg, #1e3c72, #2a5298); color: white; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 40px; }
        .header h1 { font-size: 2.5em; margin: 0; color: #00ff88; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); 
                       gap: 20px; margin-bottom: 30px; }
        .status-card { background: rgba(255,255,255,0.1); border-radius: 15px; padding: 20px; 
                       backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.2); }
        .status-title { font-size: 1.3em; font-weight: bold; margin-bottom: 15px; color: #00ff88; }
        .status-item { display: flex; justify-content: space-between; margin: 10px 0; 
                       padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .status-value { font-weight: bold; color: #ffeb3b; }
        .positions-table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        .positions-table th, .positions-table td { padding: 12px; text-align: left; 
                                                    border-bottom: 1px solid rgba(255,255,255,0.2); }
        .positions-table th { background: rgba(0,255,136,0.2); color: #00ff88; }
        .profit { color: #4caf50; }
        .loss { color: #f44336; }
        .footer { text-align: center; margin-top: 40px; color: rgba(255,255,255,0.7); }
    """
    
    # HTML 템플릿 (수정된 버전)
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Phoenix 95 V4 Execute Service</title>
        <style>
            {css_styles}
        </style>
        <script>
            setInterval(() => location.reload(), 10000); // 10초마다 새로고침
        </script>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>⚡ Phoenix 95 V4 Execute Service</h1>
                <p>헤지펀드급 20x 레버리지 거래 실행 시스템</p>
                <p>포트: 8102 | 상태: <span style="color: #00ff88;">ACTIVE</span></p>
            </div>
            
            <div class="status-grid">
                <div class="status-card">
                    <div class="status-title">🔥 실행 통계</div>
                    <div class="status-item">
                        <span>총 실행 횟수:</span>
                        <span class="status-value">{execute_engine.execution_stats['total_executions']:,}</span>
                    </div>
                    <div class="status-item">
                        <span>성공 실행:</span>
                        <span class="status-value">{execute_engine.execution_stats['successful_executions']:,}</span>
                    </div>
                    <div class="status-item">
                        <span>실패 실행:</span>
                        <span class="status-value">{execute_engine.execution_stats['failed_executions']:,}</span>
                    </div>
                    <div class="status-item">
                        <span>평균 실행시간:</span>
                        <span class="status-value">{execute_engine.execution_stats['avg_execution_time']:.1f}ms</span>
                    </div>
                </div>
                
                <div class="status-card">
                    <div class="status-title">💰 포지션 현황</div>
                    <div class="status-item">
                        <span>활성 포지션:</span>
                        <span class="status-value">{len(execute_engine.active_positions)}</span>
                    </div>
                    <div class="status-item">
                        <span>일일 P&L:</span>
                        <span class="status-value" style="color: {pnl_color}">${execute_engine.daily_pnl:+,.2f}</span>
                    </div>
                    <div class="status-item">
                        <span>총 거래 수:</span>
                        <span class="status-value">{execute_engine.total_trades}</span>
                    </div>
                    <div class="status-item">
                        <span>승률:</span>
                        <span class="status-value">{execute_engine.execution_stats['win_rate']:.1f}%</span>
                    </div>
                </div>
                
                <div class="status-card">
                    <div class="status-title">⚙️ 시스템 설정</div>
                    <div class="status-item">
                        <span>레버리지:</span>
                        <span class="status-value">{config.LEVERAGE}x</span>
                    </div>
                    <div class="status-item">
                        <span>마진 모드:</span>
                        <span class="status-value">{config.MARGIN_MODE}</span>
                    </div>
                    <div class="status-item">
                        <span>손절:</span>
                        <span class="status-value">{config.STOP_LOSS_PERCENT*100:.0f}%</span>
                    </div>
                    <div class="status-item">
                        <span>익절:</span>
                        <span class="status-value">{config.TAKE_PROFIT_PERCENT*100:.0f}%</span>
                    </div>
                </div>
                
                <div class="status-card">
                    <div class="status-title">🛡️ 리스크 관리</div>
                    <div class="status-item">
                        <span>최대 포지션:</span>
                        <span class="status-value">{config.MAX_POSITIONS}</span>
                    </div>
                    <div class="status-item">
                        <span>일일 손실 한도:</span>
                        <span class="status-value">{config.MAX_DAILY_LOSS*100:.0f}%</span>
                    </div>
                    <div class="status-item">
                        <span>청산 경고:</span>
                        <span class="status-value">{execute_engine.risk_metrics['liquidation_warnings']}</span>
                    </div>
                    <div class="status-item">
                        <span>최소 신뢰도:</span>
                        <span class="status-value">{config.MIN_CONFIDENCE*100:.0f}%</span>
                    </div>
                </div>
            </div>
            
            {positions_html}
            
            <div class="footer">
                <p>Phoenix 95 V4 Execute Service | 마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <p>⚡ 20x 레버리지 | 🏛️ 헤지펀드급 | 🛡️ 이솔레이티드 마진</p>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/execute")
async def execute_trade(request: ExecuteRequest):
    """거래 실행 API"""
    try:
        # TradingSignal 객체 생성
        signal = TradingSignal(
            signal_id=request.signal_id,
            symbol=request.symbol,
            action=request.action,
            price=request.price,
            confidence=request.confidence,
            phoenix95_score=request.phoenix95_score,
            kelly_ratio=request.kelly_ratio
        )
        
        # 거래 실행
        result = await execute_engine.execute_trade_complete(signal)
        
        return {
            "status": "success",
            "execution_result": asdict(result),
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"❌ API 거래 실행 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """헬스체크 API"""
    return {
        "status": "healthy",
        "service": "Phoenix95-Execute-V4",
        "port": 8102,
        "timestamp": time.time(),
        "system_status": execute_engine.get_system_status()
    }

@app.get("/positions")
async def get_positions():
    """활성 포지션 조회 API"""
    try:
        positions = {
            pos_id: asdict(pos) for pos_id, pos in execute_engine.active_positions.items()
        }
        
        return {
            "status": "success",
            "active_positions": positions,
            "position_count": len(positions),
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"❌ 포지션 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/positions/{position_id}/close")
async def close_position(position_id: str):
    """수동 포지션 청산 API"""
    try:
        if position_id not in execute_engine.active_positions:
            raise HTTPException(status_code=404, detail="포지션을 찾을 수 없습니다")
        
        position = execute_engine.active_positions[position_id]
        current_price = await execute_engine._get_current_price(position.symbol)
        
        await execute_engine._close_position(position, current_price, "수동 청산")
        
        return {
            "status": "success",
            "message": f"포지션 {position_id} 청산 완료",
            "timestamp": time.time()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 수동 청산 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
async def get_statistics():
    """상세 통계 API"""
    try:
        return {
            "status": "success",
            "statistics": {
                "execution_stats": execute_engine.execution_stats,
                "risk_metrics": execute_engine.risk_metrics,
                "system_info": {
                    "active_positions": len(execute_engine.active_positions),
                    "total_trades": execute_engine.total_trades,
                    "successful_trades": execute_engine.successful_trades,
                    "daily_pnl": execute_engine.daily_pnl,
                    "position_history_count": len(execute_engine.position_history)
                },
                "config": {
                    "leverage": config.LEVERAGE,
                    "margin_mode": config.MARGIN_MODE,
                    "stop_loss_percent": config.STOP_LOSS_PERCENT,
                    "take_profit_percent": config.TAKE_PROFIT_PERCENT,
                    "max_positions": config.MAX_POSITIONS,
                    "max_daily_loss": config.MAX_DAILY_LOSS,
                    "min_confidence": config.MIN_CONFIDENCE
                }
            },
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"❌ 통계 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════════════════════════
#                              🚀 메인 실행부
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("🚀 Phoenix 95 V4 Execute Service 시작")
    logger.info(f"   포트: 8102")
    logger.info(f"   레버리지: {config.LEVERAGE}x {config.MARGIN_MODE}")
    logger.info(f"   익절/손절: ±{config.TAKE_PROFIT_PERCENT*100:.0f}%")
    logger.info(f"   최대 포지션: {config.MAX_POSITIONS}개")
    logger.info(f"   일일 손실 한도: {config.MAX_DAILY_LOSS*100:.0f}%")
    
    try:
        uvicorn.run(
            "execute_service:app",
            host="0.0.0.0",
            port=8102,
            reload=False,
            log_level="info",
            access_log=True
        )
    except KeyboardInterrupt:
        logger.info("🛑 서비스 중단 요청")
    except Exception as e:
        logger.error(f"❌ 서비스 실행 오류: {e}")
    finally:
        logger.info("👋 Phoenix 95 V4 Execute Service 종료")