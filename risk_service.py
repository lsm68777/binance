#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🛡️ Phoenix 95 V4 RISK Service - 헤지펀드급 리스크 관리 시스템
포트: 8101
역할: 20x 레버리지 리스크 관리, Kelly Criterion, 자동 포지션 관리
"""

import asyncio
import json
import time
import logging
import traceback
import aiohttp
import aioredis
import asyncpg
import numpy as np
import pandas as pd
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from collections import deque
import hashlib
import hmac
import os
import sys
import signal
import uuid

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# =============================================================================
# 📊 RISK 서비스 설정
# =============================================================================

RISK_CONFIG = {
    "service_name": "Phoenix95_Risk_Guardian",
    "version": "4.0.0-leverage",
    "port": 8101,
    "host": "0.0.0.0",
    
    # 리스크 한도 설정
    "max_leverage": 20,                    # 최대 20x 레버리지
    "margin_mode": "ISOLATED",             # 이솔레이티드 마진
    "max_daily_loss": 0.02,               # 일일 최대 손실 2%
    "max_position_size": 0.05,            # 최대 포지션 크기 5%
    "max_positions": 5,                   # 최대 동시 포지션 5개
    "confidence_threshold": 0.75,         # 최소 신뢰도 75%
    
    # Kelly Criterion 설정
    "kelly_max_fraction": 0.25,           # Kelly 최대 25%
    "kelly_min_fraction": 0.01,           # Kelly 최소 1%
    "win_rate_adjustment": 0.9,           # 승률 조정 계수
    
    # 손절/익절 설정 (고정)
    "stop_loss_percent": 0.02,            # 2% 손절
    "take_profit_percent": 0.02,          # 2% 익절
    "liquidation_buffer": 0.1,            # 청산 버퍼 10%
    
    # 모니터링 설정
    "position_check_interval": 0.5,       # 0.5초마다 포지션 체크
    "risk_alert_threshold": 0.8,          # 80% 리스크시 알림
    "emergency_close_threshold": 0.95,    # 95% 리스크시 강제 청산
    
    # 성능 설정
    "max_memory_usage": 0.85,             # 85% 메모리 사용시 정리
    "cache_ttl": 300,                     # 5분 캐시
    "max_cache_size": 10000
}

# 텔레그램 설정
TELEGRAM_CONFIG = {
    "token": "7386542811:AAEZ21p30rES1k8NxNM2xbZ53U44PI9D5CY",
    "chat_id": "7590895952",
    "enabled": True
}

# 데이터베이스 설정
DATABASE_CONFIG = {
    "redis_url": "redis://localhost:6379",
    "postgres_url": "postgresql://postgres:password@localhost:5432/phoenix95_risk",
    "connection_pool_size": 20
}

# =============================================================================
# 📊 리스크 데이터 모델
# =============================================================================

@dataclass
class RiskProfile:
    """리스크 프로필"""
    symbol: str
    leverage: int
    position_size: float
    margin_required: float
    liquidation_price: float
    stop_loss_price: float
    take_profit_price: float
    risk_score: float
    confidence: float
    created_at: datetime = field(default_factory=datetime.utcnow)

@dataclass
class PositionRisk:
    """포지션 리스크"""
    position_id: str
    symbol: str
    side: str  # BUY/SELL
    entry_price: float
    current_price: float
    quantity: float
    leverage: int
    margin_required: float
    unrealized_pnl: float
    liquidation_risk: float
    distance_to_liquidation: float
    status: str = "ACTIVE"
    last_updated: datetime = field(default_factory=datetime.utcnow)

@dataclass
class RiskMetrics:
    """리스크 메트릭"""
    timestamp: datetime
    total_exposure: float
    margin_utilization: float
    portfolio_var: float
    max_drawdown: float
    active_positions: int
    daily_pnl: float
    leverage_ratio: float
    risk_level: str

# =============================================================================
# 🛡️ Kelly Criterion 계산기
# =============================================================================

class KellyCriterionCalculator:
    """헤지펀드급 Kelly Criterion 계산기"""
    
    def __init__(self):
        self.historical_performance = deque(maxlen=1000)
        self.win_rates = {}
        self.profit_loss_ratios = {}
        
    def calculate_kelly_fraction(self, signal_data: Dict, confidence: float, 
                                symbol: str) -> Dict[str, float]:
        """Kelly Criterion 포지션 크기 계산"""
        try:
            # 기본 파라미터
            win_rate = self._estimate_win_rate(symbol, confidence)
            profit_loss_ratio = self._estimate_profit_loss_ratio(symbol)
            volatility_adjustment = self._calculate_volatility_adjustment(symbol)
            
            # Kelly 공식: f* = (bp - q) / b
            # f* = kelly fraction, b = profit_loss_ratio, p = win_rate, q = loss_rate
            loss_rate = 1 - win_rate
            kelly_fraction = (profit_loss_ratio * win_rate - loss_rate) / profit_loss_ratio
            
            # 변동성 조정
            kelly_fraction *= volatility_adjustment
            
            # 안전 한계 적용
            kelly_fraction = max(
                RISK_CONFIG["kelly_min_fraction"],
                min(kelly_fraction, RISK_CONFIG["kelly_max_fraction"])
            )
            
            return {
                "kelly_fraction": kelly_fraction,
                "win_rate": win_rate,
                "profit_loss_ratio": profit_loss_ratio,
                "volatility_adjustment": volatility_adjustment,
                "confidence_boost": confidence * 0.1,
                "final_position_size": min(kelly_fraction * 1.2, RISK_CONFIG["max_position_size"])
            }
            
        except Exception as e:
            logging.error(f"Kelly 계산 실패: {e}")
            return {
                "kelly_fraction": RISK_CONFIG["kelly_min_fraction"],
                "win_rate": 0.5,
                "profit_loss_ratio": 1.0,
                "final_position_size": RISK_CONFIG["kelly_min_fraction"]
            }
    
    def _estimate_win_rate(self, symbol: str, confidence: float) -> float:
        """승률 추정"""
        # 심볼별 기본 승률
        base_win_rates = {
            "BTCUSDT": 0.65, "ETHUSDT": 0.62, "BNBUSDT": 0.58,
            "ADAUSDT": 0.55, "DOGEUSDT": 0.52, "XRPUSDT": 0.60
        }
        
        base_rate = base_win_rates.get(symbol, 0.55)
        
        # 신뢰도 기반 조정
        confidence_adjusted_rate = base_rate + (confidence - 0.5) * 0.3
        
        # 시간대 조정
        hour = datetime.utcnow().hour
        time_adjustment = 1.0
        if 8 <= hour <= 16:  # 아시아/유럽 활성 시간
            time_adjustment = 1.05
        elif 2 <= hour <= 6:  # 저조한 시간
            time_adjustment = 0.95
        
        final_rate = confidence_adjusted_rate * time_adjustment
        return max(0.3, min(0.8, final_rate))
    
    def _estimate_profit_loss_ratio(self, symbol: str) -> float:
        """손익비 추정"""
        # 2% 익절, 2% 손절이므로 기본 1:1 비율
        base_ratio = RISK_CONFIG["take_profit_percent"] / RISK_CONFIG["stop_loss_percent"]
        
        # 심볼별 조정
        symbol_adjustments = {
            "BTCUSDT": 1.1, "ETHUSDT": 1.05, "BNBUSDT": 1.0,
            "ADAUSDT": 0.95, "DOGEUSDT": 0.9
        }
        
        adjustment = symbol_adjustments.get(symbol, 1.0)
        return base_ratio * adjustment
    
    def _calculate_volatility_adjustment(self, symbol: str) -> float:
        """변동성 조정 계수"""
        # 심볼별 변동성 (높을수록 포지션 크기 축소)
        volatility_levels = {
            "BTCUSDT": 0.3, "ETHUSDT": 0.4, "BNBUSDT": 0.5,
            "ADAUSDT": 0.6, "DOGEUSDT": 0.8, "XRPUSDT": 0.5
        }
        
        volatility = volatility_levels.get(symbol, 0.5)
        
        # 변동성이 높을수록 포지션 크기 축소
        adjustment = 1.0 - (volatility * 0.3)
        return max(0.5, min(1.2, adjustment))

# =============================================================================
# ⚖️ 레버리지 리스크 관리자
# =============================================================================

class LeverageRiskManager:
    """헤지펀드급 레버리지 리스크 관리"""
    
    def __init__(self):
        self.active_positions = {}
        self.risk_history = deque(maxlen=10000)
        self.daily_metrics = {}
        self.emergency_mode = False
        
    async def validate_position_risk(self, signal_data: Dict, 
                                   kelly_result: Dict) -> Dict[str, Any]:
        """포지션 리스크 검증"""
        try:
            symbol = signal_data["symbol"]
            action = signal_data["action"].lower()
            price = float(signal_data["price"])
            confidence = signal_data.get("confidence", 0.8)
            
            # 기본 검증
            basic_validation = self._basic_risk_validation(signal_data, confidence)
            if not basic_validation["approved"]:
                return basic_validation
            
            # 레버리지 계산
            leverage = self._calculate_optimal_leverage(confidence, symbol)
            position_size = kelly_result["final_position_size"]
            
            # 포지션 정보 계산
            notional_value = position_size * 10000  # 가상 자본 $10,000
            margin_required = notional_value / leverage
            
            # 청산가 계산
            liquidation_price = self._calculate_liquidation_price(
                price, action, leverage
            )
            
            # 손절/익절가 계산 (2% 고정)
            if action in ["buy", "long"]:
                stop_loss_price = price * (1 - RISK_CONFIG["stop_loss_percent"])
                take_profit_price = price * (1 + RISK_CONFIG["take_profit_percent"])
            else:
                stop_loss_price = price * (1 + RISK_CONFIG["stop_loss_percent"])
                take_profit_price = price * (1 - RISK_CONFIG["take_profit_percent"])
            
            # 리스크 점수 계산
            risk_score = self._calculate_position_risk_score(
                leverage, margin_required, liquidation_price, price, confidence
            )
            
            # 포트폴리오 리스크 체크
            portfolio_risk = await self._check_portfolio_risk(
                notional_value, margin_required
            )
            
            if portfolio_risk["risk_level"] == "HIGH":
                return {
                    "approved": False,
                    "reason": f"포트폴리오 리스크 초과: {portfolio_risk['reason']}",
                    "risk_level": "HIGH"
                }
            
            # 최종 승인 결정
            if risk_score <= 0.8 and confidence >= RISK_CONFIG["confidence_threshold"]:
                return {
                    "approved": True,
                    "risk_profile": RiskProfile(
                        symbol=symbol,
                        leverage=leverage,
                        position_size=position_size,
                        margin_required=margin_required,
                        liquidation_price=liquidation_price,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        risk_score=risk_score,
                        confidence=confidence
                    ),
                    "execution_params": {
                        "leverage": leverage,
                        "position_size": position_size,
                        "margin_required": margin_required,
                        "stop_loss_percent": RISK_CONFIG["stop_loss_percent"] * 100,
                        "take_profit_percent": RISK_CONFIG["take_profit_percent"] * 100,
                    },
                    "risk_metrics": {
                        "risk_score": risk_score,
                        "liquidation_distance": abs(price - liquidation_price) / price,
                        "margin_ratio": margin_required / 10000,
                        "kelly_confidence": kelly_result["kelly_fraction"]
                    }
                }
            else:
                return {
                    "approved": False,
                    "reason": f"리스크 점수 초과: {risk_score:.2f} (한도: 0.8) 또는 신뢰도 부족: {confidence:.2f}",
                    "risk_level": "HIGH" if risk_score > 0.8 else "MEDIUM"
                }
                
        except Exception as e:
            logging.error(f"포지션 리스크 검증 실패: {e}")
            return {
                "approved": False,
                "reason": f"리스크 검증 오류: {str(e)}",
                "risk_level": "UNKNOWN"
            }
    
    def _basic_risk_validation(self, signal_data: Dict, confidence: float) -> Dict:
        """기본 리스크 검증"""
        # 최대 포지션 수 체크
        if len(self.active_positions) >= RISK_CONFIG["max_positions"]:
            return {
                "approved": False,
                "reason": f"최대 포지션 수 초과: {len(self.active_positions)}/{RISK_CONFIG['max_positions']}"
            }
        
        # 신뢰도 체크
        if confidence < RISK_CONFIG["confidence_threshold"]:
            return {
                "approved": False,
                "reason": f"신뢰도 부족: {confidence:.2f} < {RISK_CONFIG['confidence_threshold']}"
            }
        
        # 심볼 중복 체크
        symbol = signal_data["symbol"]
        existing_positions = [p for p in self.active_positions.values() 
                            if p.symbol == symbol]
        if len(existing_positions) >= 2:
            return {
                "approved": False,
                "reason": f"심볼 {symbol} 포지션 중복 (최대 2개)"
            }
        
        # 일일 손실 한도 체크
        today_pnl = self._get_daily_pnl()
        if today_pnl <= -RISK_CONFIG["max_daily_loss"]:
            return {
                "approved": False,
                "reason": f"일일 손실 한도 초과: {today_pnl:.1%}"
            }
        
        return {"approved": True, "reason": "기본 검증 통과"}
    
    def _calculate_optimal_leverage(self, confidence: float, symbol: str) -> int:
        """최적 레버리지 계산"""
        max_leverage = RISK_CONFIG["max_leverage"]
        
        # 신뢰도 기반 레버리지
        if confidence >= 0.9:
            leverage = max_leverage
        elif confidence >= 0.8:
            leverage = int(max_leverage * 0.8)
        elif confidence >= 0.75:
            leverage = int(max_leverage * 0.6)
        else:
            leverage = int(max_leverage * 0.4)
        
        # 심볼별 제한
        symbol_max_leverage = {
            "BTCUSDT": 125, "ETHUSDT": 100, "BNBUSDT": 50,
            "ADAUSDT": 25, "DOGEUSDT": 20, "XRPUSDT": 50
        }
        
        symbol_limit = symbol_max_leverage.get(symbol, max_leverage)
        leverage = min(leverage, symbol_limit)
        
        return max(2, leverage)  # 최소 2x
    
    def _calculate_liquidation_price(self, entry_price: float, action: str, 
                                   leverage: int) -> float:
        """청산가 계산"""
        maintenance_margin_rate = 0.004  # 0.4% 유지마진
        
        if action in ["buy", "long"]:
            # 롱 포지션 청산가
            liquidation_price = entry_price * (
                1 - (1/leverage) + maintenance_margin_rate
            )
        else:
            # 숏 포지션 청산가
            liquidation_price = entry_price * (
                1 + (1/leverage) - maintenance_margin_rate
            )
        
        return max(0, liquidation_price)
    
    def _calculate_position_risk_score(self, leverage: int, margin_required: float,
                                     liquidation_price: float, entry_price: float,
                                     confidence: float) -> float:
        """포지션 리스크 점수 계산 (0-1, 낮을수록 안전)"""
        risk_factors = []
        
        # 레버리지 리스크
        leverage_risk = min(leverage / RISK_CONFIG["max_leverage"], 1.0)
        risk_factors.append(leverage_risk * 0.3)
        
        # 마진 비율 리스크
        margin_ratio = margin_required / 10000  # 가상 자본 대비
        margin_risk = min(margin_ratio / RISK_CONFIG["max_position_size"], 1.0)
        risk_factors.append(margin_risk * 0.25)
        
        # 청산가 근접도 리스크
        liquidation_distance = abs(entry_price - liquidation_price) / entry_price
        liquidation_risk = max(0, 1 - liquidation_distance / 0.1)  # 10% 이상 거리면 안전
        risk_factors.append(liquidation_risk * 0.2)
        
        # 신뢰도 역리스크
        confidence_risk = 1 - confidence
        risk_factors.append(confidence_risk * 0.15)
        
        # 시장 시간 리스크
        hour = datetime.utcnow().hour
        if 2 <= hour <= 6:  # 저조한 시간
            risk_factors.append(0.1)
        
        total_risk = sum(risk_factors)
        return min(1.0, total_risk)
    
    async def _check_portfolio_risk(self, new_notional: float, 
                                  new_margin: float) -> Dict:
        """포트폴리오 전체 리스크 체크"""
        try:
            current_exposure = sum(
                pos.quantity * pos.current_price 
                for pos in self.active_positions.values()
            )
            
            current_margin = sum(
                pos.margin_required 
                for pos in self.active_positions.values()
            )
            
            total_exposure = current_exposure + new_notional
            total_margin = current_margin + new_margin
            
            # 총 노출 한도 체크 (자본의 200%)
            if total_exposure > 20000:  # $10,000 * 2
                return {
                    "risk_level": "HIGH",
                    "reason": f"총 노출 한도 초과: ${total_exposure:,.0f}"
                }
            
            # 마진 사용률 체크 (자본의 80%)
            if total_margin > 8000:  # $10,000 * 0.8
                return {
                    "risk_level": "HIGH", 
                    "reason": f"마진 사용률 초과: ${total_margin:,.0f}"
                }
            
            return {
                "risk_level": "LOW",
                "total_exposure": total_exposure,
                "total_margin": total_margin,
                "exposure_ratio": total_exposure / 10000,
                "margin_ratio": total_margin / 10000
            }
            
        except Exception as e:
            logging.error(f"포트폴리오 리스크 체크 실패: {e}")
            return {"risk_level": "MEDIUM", "reason": "리스크 체크 실패"}
    
    def _get_daily_pnl(self) -> float:
        """일일 P&L 조회"""
        today = datetime.utcnow().date()
        daily_pnl = 0.0
        
        for position in self.active_positions.values():
            if position.last_updated.date() == today:
                daily_pnl += position.unrealized_pnl
        
        # 청산된 포지션의 realized PnL도 포함해야 하지만 
        # 간단히 하기 위해 현재 포지션만 계산
        return daily_pnl / 10000  # 비율로 변환

# =============================================================================
# 📊 실시간 포지션 모니터
# =============================================================================

class RealTimePositionMonitor:
    """실시간 포지션 모니터링"""
    
    def __init__(self, telegram_notifier):
        self.active_monitors = {}
        self.telegram = telegram_notifier
        self.price_cache = {}
        self.alert_history = deque(maxlen=1000)
        
    async def start_position_monitoring(self, position_id: str, 
                                      risk_profile: RiskProfile):
        """포지션 모니터링 시작"""
        position_risk = PositionRisk(
            position_id=position_id,
            symbol=risk_profile.symbol,
            side="BUY" if "buy" in risk_profile.symbol.lower() else "SELL",
            entry_price=0,  # 실제 거래 시 설정
            current_price=0,
            quantity=risk_profile.position_size,
            leverage=risk_profile.leverage,
            margin_required=risk_profile.margin_required,
            unrealized_pnl=0,
            liquidation_risk=0,
            distance_to_liquidation=0
        )
        
        # 모니터링 태스크 시작
        monitor_task = asyncio.create_task(
            self._monitor_position(position_id, position_risk, risk_profile)
        )
        self.active_monitors[position_id] = monitor_task
        
        logging.info(f"🔍 포지션 모니터링 시작: {position_id} ({risk_profile.symbol})")
    
    async def _monitor_position(self, position_id: str, position_risk: PositionRisk,
                              risk_profile: RiskProfile):
        """개별 포지션 모니터링"""
        try:
            while position_id in self.active_monitors:
                # 현재 가격 조회
                current_price = await self._get_current_price(position_risk.symbol)
                if current_price is None:
                    await asyncio.sleep(5)
                    continue
                
                # 포지션 업데이트
                position_risk.current_price = current_price
                position_risk.last_updated = datetime.utcnow()
                
                # P&L 계산
                if position_risk.entry_price > 0:
                    if position_risk.side == "BUY":
                        price_change = (current_price - position_risk.entry_price) / position_risk.entry_price
                    else:
                        price_change = (position_risk.entry_price - current_price) / position_risk.entry_price
                    
                    # 레버리지 적용 P&L
                    leveraged_return = price_change * position_risk.leverage
                    position_risk.unrealized_pnl = position_risk.margin_required * leveraged_return
                    
                    # 청산 리스크 계산
                    position_risk.distance_to_liquidation = abs(
                        current_price - risk_profile.liquidation_price
                    ) / current_price
                    
                    position_risk.liquidation_risk = max(
                        0, 1 - position_risk.distance_to_liquidation / 0.05
                    )  # 5% 이내 접근시 높은 리스크
                    
                    # 리스크 알림 체크
                    await self._check_risk_alerts(position_id, position_risk, risk_profile)
                    
                    # 자동 청산 조건 체크
                    should_close = self._check_auto_close_conditions(
                        position_risk, risk_profile, current_price
                    )
                    
                    if should_close["should_close"]:
                        await self._execute_auto_close(
                            position_id, position_risk, should_close["reason"]
                        )
                        break
                
                # 체크 간격
                await asyncio.sleep(RISK_CONFIG["position_check_interval"])
                
        except Exception as e:
            logging.error(f"포지션 모니터링 오류 ({position_id}): {e}")
        finally:
            if position_id in self.active_monitors:
                del self.active_monitors[position_id]
    
    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """현재 가격 조회 (캐시 사용)"""
        cache_key = f"price_{symbol}"
        cache_time = self.price_cache.get(f"{cache_key}_time", 0)
        
        if time.time() - cache_time < 5:  # 5초 캐시
            return self.price_cache.get(cache_key)
        
        try:
            # 실제로는 바이낸스 API 호출
            # 여기서는 시뮬레이션을 위해 랜덤 변동
            import random
            base_prices = {
                "BTCUSDT": 45000, "ETHUSDT": 3000, "BNBUSDT": 300,
                "ADAUSDT": 0.5, "DOGEUSDT": 0.08, "XRPUSDT": 0.6
            }
            
            base_price = base_prices.get(symbol, 1000)
            variation = random.uniform(-0.02, 0.02)  # ±2% 변동
            current_price = base_price * (1 + variation)
            
            self.price_cache[cache_key] = current_price
            self.price_cache[f"{cache_key}_time"] = time.time()
            
            return current_price
            
        except Exception as e:
            logging.error(f"가격 조회 실패 ({symbol}): {e}")
            return None
    
    async def _check_risk_alerts(self, position_id: str, position_risk: PositionRisk,
                               risk_profile: RiskProfile):
        """리스크 알림 체크"""
        alert_needed = False
        alert_level = "INFO"
        alert_message = ""
        
        # 청산 위험 알림
        if position_risk.liquidation_risk >= RISK_CONFIG["emergency_close_threshold"]:
            alert_needed = True
            alert_level = "CRITICAL"
            alert_message = f"🚨 긴급 청산 위험: {position_risk.symbol}"
            
        elif position_risk.liquidation_risk >= RISK_CONFIG["risk_alert_threshold"]:
            alert_needed = True
            alert_level = "WARNING"
            alert_message = f"⚠️ 청산 위험 증가: {position_risk.symbol}"
        
        # 큰 손실 알림
        if position_risk.unrealized_pnl < -position_risk.margin_required * 0.5:
            alert_needed = True
            alert_level = "WARNING"
            alert_message = f"📉 큰 손실: {position_risk.symbol} P&L: ${position_risk.unrealized_pnl:.2f}"
        
        # 중복 알림 방지
        alert_key = f"{position_id}_{alert_level}_{int(time.time() / 300)}"  # 5분 단위
        if alert_needed and alert_key not in [a["key"] for a in self.alert_history]:
            await self.telegram.send_risk_alert(
                position_id, position_risk, alert_level, alert_message
            )
            
            self.alert_history.append({
                "key": alert_key,
                "timestamp": time.time(),
                "level": alert_level,
                "message": alert_message
            })
    
    def _check_auto_close_conditions(self, position_risk: PositionRisk,
                                   risk_profile: RiskProfile, 
                                   current_price: float) -> Dict:
        """자동 청산 조건 체크"""
        # 긴급 청산 (청산가 5% 이내)
        if position_risk.liquidation_risk >= RISK_CONFIG["emergency_close_threshold"]:
            return {
                "should_close": True,
                "reason": "EMERGENCY_LIQUIDATION_RISK"
            }
        
        # 손절선 도달
        if position_risk.side == "BUY":
            if current_price <= risk_profile.stop_loss_price:
                return {"should_close": True, "reason": "STOP_LOSS_TRIGGERED"}
        else:
            if current_price >= risk_profile.stop_loss_price:
                return {"should_close": True, "reason": "STOP_LOSS_TRIGGERED"}
        
        # 익절선 도달
        if position_risk.side == "BUY":
            if current_price >= risk_profile.take_profit_price:
                return {"should_close": True, "reason": "TAKE_PROFIT_TRIGGERED"}
        else:
            if current_price <= risk_profile.take_profit_price:
                return {"should_close": True, "reason": "TAKE_PROFIT_TRIGGERED"}
        
        # 시간 기반 청산 (24시간)
        position_age = (datetime.utcnow() - position_risk.last_updated).total_seconds()
        if position_age > 86400:  # 24시간
            return {"should_close": True, "reason": "TIME_BASED_CLOSE"}
        
        return {"should_close": False, "reason": "NO_CLOSE_CONDITION"}
    
    async def _execute_auto_close(self, position_id: str, position_risk: PositionRisk,
                                reason: str):
        """자동 청산 실행"""
        try:
            logging.info(f"🔒 자동 청산 실행: {position_id} - {reason}")
            
            # 실제로는 거래소 API 호출하여 포지션 청산
            # 여기서는 시뮬레이션
            
            final_pnl = position_risk.unrealized_pnl
            
            # 텔레그램 알림
            await self.telegram.send_position_closed(
                position_id, position_risk, reason, final_pnl
            )
            
            # 모니터링 중지
            if position_id in self.active_monitors:
                self.active_monitors[position_id].cancel()
                del self.active_monitors[position_id]
            
            logging.info(f"✅ 포지션 청산 완료: {position_id}")
            
        except Exception as e:
            logging.error(f"자동 청산 실행 실패 ({position_id}): {e}")

# =============================================================================
# 📱 텔레그램 알림 시스템
# =============================================================================

class RiskTelegramNotifier:
    """리스크 전용 텔레그램 알림"""
    
    def __init__(self):
        self.token = TELEGRAM_CONFIG["token"]
        self.chat_id = TELEGRAM_CONFIG["chat_id"]
        self.enabled = TELEGRAM_CONFIG["enabled"]
    
    async def send_risk_approval(self, signal_data: Dict, risk_profile: RiskProfile,
                               kelly_result: Dict):
        """리스크 승인 알림"""
        if not self.enabled:
            return
        
        message = f"""🛡️ <b>리스크 승인 - 거래 허가</b>

📊 <b>신호 정보</b>
• 심볼: <code>{signal_data['symbol']}</code>
• 액션: <b>{signal_data['action'].upper()}</b>
• 가격: <code>${signal_data['price']:,.2f}</code>
• 신뢰도: <b>{signal_data.get('confidence', 0.8):.1%}</b>

⚡ <b>레버리지 설정</b>
• 레버리지: <b>{risk_profile.leverage}x ISOLATED</b>
• 포지션 크기: <code>${risk_profile.position_size * 10000:,.2f}</code>
• 필요 마진: <code>${risk_profile.margin_required:,.2f}</code>

🎯 <b>손익 설정</b>
• 익절가: <code>${risk_profile.take_profit_price:,.2f}</code> (+2%)
• 손절가: <code>${risk_profile.stop_loss_price:,.2f}</code> (-2%)
• 청산가: <code>${risk_profile.liquidation_price:,.2f}</code>

📈 <b>Kelly Criterion</b>
• Kelly 비율: <b>{kelly_result['kelly_fraction']:.1%}</b>
• 승률 추정: <b>{kelly_result['win_rate']:.1%}</b>
• 손익비: <b>{kelly_result['profit_loss_ratio']:.2f}</b>

🛡️ <b>리스크 점수: {risk_profile.risk_score:.1%}</b>

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        await self._send_message(message)
    
    async def send_risk_rejection(self, signal_data: Dict, reason: str, risk_level: str):
        """리스크 거부 알림"""
        if not self.enabled:
            return
        
        emoji = "🚫" if risk_level == "HIGH" else "⚠️"
        
        message = f"""{emoji} <b>리스크 거부 - 거래 차단</b>

📊 <b>신호 정보</b>
• 심볼: <code>{signal_data['symbol']}</code>
• 액션: <b>{signal_data['action'].upper()}</b>
• 가격: <code>${signal_data['price']:,.2f}</code>

❌ <b>거부 사유</b>
{reason}

🛡️ <b>리스크 레벨: {risk_level}</b>

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        await self._send_message(message)
    
    async def send_risk_alert(self, position_id: str, position_risk: PositionRisk,
                            alert_level: str, alert_message: str):
        """리스크 알림"""
        if not self.enabled:
            return
        
        emoji_map = {
            "INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"
        }
        emoji = emoji_map.get(alert_level, "📢")
        
        message = f"""{emoji} <b>리스크 알림 - {alert_level}</b>

{alert_message}

📊 <b>포지션 상태</b>
• ID: <code>{position_id}</code>
• 심볼: <code>{position_risk.symbol}</code>
• 현재가: <code>${position_risk.current_price:,.2f}</code>
• 미실현 P&L: <code>${position_risk.unrealized_pnl:,.2f}</code>
• 청산 위험도: <b>{position_risk.liquidation_risk:.1%}</b>
• 청산까지 거리: <b>{position_risk.distance_to_liquidation:.1%}</b>

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        await self._send_message(message)
    
    async def send_position_closed(self, position_id: str, position_risk: PositionRisk,
                                 reason: str, final_pnl: float):
        """포지션 청산 알림"""
        if not self.enabled:
            return
        
        emoji = "📈" if final_pnl > 0 else "📉"
        
        message = f"""{emoji} <b>포지션 청산 완료</b>

📊 <b>청산 정보</b>
• ID: <code>{position_id}</code>
• 심볼: <code>{position_risk.symbol}</code>
• 청산 사유: <b>{reason}</b>
• 최종 P&L: <code>${final_pnl:,.2f}</code>

📈 <b>포지션 요약</b>
• 레버리지: <b>{position_risk.leverage}x</b>
• 마진: <code>${position_risk.margin_required:,.2f}</code>
• ROE: <b>{(final_pnl / position_risk.margin_required * 100):+.1f}%</b>

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        await self._send_message(message)
    
    async def send_system_status(self, metrics: Dict):
        """시스템 상태 알림"""
        if not self.enabled:
            return
        
        message = f"""📊 <b>리스크 시스템 상태</b>

🛡️ <b>포트폴리오 리스크</b>
• 활성 포지션: <b>{metrics.get('active_positions', 0)}개</b>
• 총 노출: <code>${metrics.get('total_exposure', 0):,.0f}</code>
• 마진 사용률: <b>{metrics.get('margin_utilization', 0):.1%}</b>
• 일일 P&L: <code>${metrics.get('daily_pnl', 0):,.2f}</code>

⚡ <b>시스템 성능</b>
• 메모리 사용률: <b>{metrics.get('memory_usage', 0):.1%}</b>
• 처리된 신호: <b>{metrics.get('signals_processed', 0)}개</b>
• 승인률: <b>{metrics.get('approval_rate', 0):.1%}</b>

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        await self._send_message(message)
    
    async def _send_message(self, message: str):
        """메시지 전송"""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=10) as response:
                    if response.status != 200:
                        logging.warning(f"텔레그램 전송 실패: {response.status}")
                        
        except Exception as e:
            logging.error(f"텔레그램 전송 오류: {e}")

# =============================================================================
# 🚀 RISK 서비스 메인 클래스
# =============================================================================

class RiskService:
    """Phoenix 95 RISK 서비스 (포트 8101)"""
    
    def __init__(self):
        self.app = FastAPI(
            title="Phoenix 95 Risk Management Service",
            description="헤지펀드급 리스크 관리 및 20x 레버리지 시스템",
            version=RISK_CONFIG["version"]
        )
        
        # CORS 설정
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # 컴포넌트 초기화
        self.kelly_calculator = KellyCriterionCalculator()
        self.risk_manager = LeverageRiskManager()
        self.telegram = RiskTelegramNotifier()
        self.position_monitor = RealTimePositionMonitor(self.telegram)
        
        # 성능 메트릭
        self.performance_metrics = {
            "start_time": time.time(),
            "total_requests": 0,
            "approved_requests": 0,
            "rejected_requests": 0,
            "active_monitors": 0,
            "system_alerts": 0
        }
        
        # 라우트 설정
        self._setup_routes()
        
        # 백그라운드 태스크
        self.background_tasks = []
        
        logging.info(f"🛡️ Phoenix 95 Risk Service v{RISK_CONFIG['version']} 초기화 완료")
    
    def _setup_routes(self):
        """API 라우트 설정"""
        
        @self.app.get("/")
        async def root():
            return HTMLResponse(self._generate_dashboard_html())
        
        @self.app.get("/health")
        async def health_check():
            uptime = time.time() - self.performance_metrics["start_time"]
            return {
                "status": "healthy",
                "service": "risk_management",
                "version": RISK_CONFIG["version"],
                "uptime_seconds": uptime,
                "active_positions": len(self.risk_manager.active_positions),
                "active_monitors": len(self.position_monitor.active_monitors),
                "memory_usage": psutil.virtual_memory().percent,
                "timestamp": time.time()
            }
        
        @self.app.post("/risk/validate")
        async def validate_risk(request: Request):
            """리스크 검증 및 승인"""
            try:
                self.performance_metrics["total_requests"] += 1
                
                data = await request.json()
                signal_data = data.get("signal_data", {})
                
                if not signal_data:
                    raise HTTPException(status_code=400, detail="신호 데이터 누락")
                
                # Kelly Criterion 계산
                kelly_result = self.kelly_calculator.calculate_kelly_fraction(
                    signal_data, 
                    signal_data.get("confidence", 0.8),
                    signal_data["symbol"]
                )
                
                # 리스크 검증
                risk_validation = await self.risk_manager.validate_position_risk(
                    signal_data, kelly_result
                )
                
                if risk_validation["approved"]:
                    self.performance_metrics["approved_requests"] += 1
                    
                    # 승인 알림
                    await self.telegram.send_risk_approval(
                        signal_data, risk_validation["risk_profile"], kelly_result
                    )
                    
                    # 포지션 모니터링 시작
                    position_id = f"POS_{int(time.time() * 1000)}"
                    await self.position_monitor.start_position_monitoring(
                        position_id, risk_validation["risk_profile"]
                    )
                    
                    # 활성 포지션에 추가
                    self.risk_manager.active_positions[position_id] = PositionRisk(
                        position_id=position_id,
                        symbol=signal_data["symbol"],
                        side="BUY" if signal_data["action"].lower() in ["buy", "long"] else "SELL",
                        entry_price=float(signal_data["price"]),
                        current_price=float(signal_data["price"]),
                        quantity=risk_validation["risk_profile"].position_size,
                        leverage=risk_validation["risk_profile"].leverage,
                        margin_required=risk_validation["risk_profile"].margin_required,
                        unrealized_pnl=0,
                        liquidation_risk=0,
                        distance_to_liquidation=1.0
                    )
                    
                    logging.info(f"✅ 리스크 승인: {signal_data['symbol']} {signal_data['action']}")
                    
                else:
                    self.performance_metrics["rejected_requests"] += 1
                    
                    # 거부 알림
                    await self.telegram.send_risk_rejection(
                        signal_data, 
                        risk_validation["reason"], 
                        risk_validation.get("risk_level", "MEDIUM")
                    )
                    
                    logging.warning(f"❌ 리스크 거부: {signal_data['symbol']} - {risk_validation['reason']}")
                
                return {
                    "status": "success",
                    "approved": risk_validation["approved"],
                    "validation_result": risk_validation,
                    "kelly_result": kelly_result,
                    "timestamp": time.time()
                }
                
            except Exception as e:
                logging.error(f"리스크 검증 실패: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.get("/risk/positions")
        async def get_active_positions():
            """활성 포지션 조회"""
            positions = {}
            for pos_id, position in self.risk_manager.active_positions.items():
                positions[pos_id] = {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "quantity": position.quantity,
                    "leverage": position.leverage,
                    "margin_required": position.margin_required,
                    "unrealized_pnl": position.unrealized_pnl,
                    "liquidation_risk": position.liquidation_risk,
                    "distance_to_liquidation": position.distance_to_liquidation,
                    "status": position.status,
                    "last_updated": position.last_updated.isoformat()
                }
            
            return {
                "active_positions": positions,
                "position_count": len(positions),
                "total_exposure": sum(p.quantity * p.current_price for p in self.risk_manager.active_positions.values()),
                "total_margin": sum(p.margin_required for p in self.risk_manager.active_positions.values()),
                "timestamp": time.time()
            }
        
        @self.app.get("/risk/metrics")
        async def get_risk_metrics():
            """리스크 메트릭 조회"""
            active_positions = list(self.risk_manager.active_positions.values())
            
            total_exposure = sum(p.quantity * p.current_price for p in active_positions)
            total_margin = sum(p.margin_required for p in active_positions)
            total_pnl = sum(p.unrealized_pnl for p in active_positions)
            
            high_risk_positions = len([p for p in active_positions if p.liquidation_risk > 0.5])
            
            memory_usage = psutil.virtual_memory().percent
            
            return {
                "portfolio_metrics": {
                    "active_positions": len(active_positions),
                    "total_exposure": total_exposure,
                    "total_margin": total_margin,
                    "margin_utilization": total_margin / 10000,  # 가상 자본 대비
                    "unrealized_pnl": total_pnl,
                    "daily_pnl": self.risk_manager._get_daily_pnl(),
                    "high_risk_positions": high_risk_positions
                },
                "system_metrics": {
                    "memory_usage": memory_usage,
                    "active_monitors": len(self.position_monitor.active_monitors),
                    "uptime_hours": (time.time() - self.performance_metrics["start_time"]) / 3600,
                    "approval_rate": (self.performance_metrics["approved_requests"] / 
                                    max(self.performance_metrics["total_requests"], 1)) * 100
                },
                "performance_metrics": self.performance_metrics,
                "timestamp": time.time()
            }
        
        @self.app.post("/risk/close_position")
        async def close_position(request: Request):
            """포지션 강제 청산"""
            try:
                data = await request.json()
                position_id = data.get("position_id")
                reason = data.get("reason", "MANUAL_CLOSE")
                
                if position_id not in self.risk_manager.active_positions:
                    raise HTTPException(status_code=404, detail="포지션을 찾을 수 없음")
                
                position = self.risk_manager.active_positions[position_id]
                
                # 강제 청산 실행
                await self.position_monitor._execute_auto_close(
                    position_id, position, reason
                )
                
                return {
                    "status": "success",
                    "message": f"포지션 {position_id} 청산 완료",
                    "reason": reason,
                    "timestamp": time.time()
                }
                
            except Exception as e:
                logging.error(f"포지션 청산 실패: {e}")
                raise HTTPException(status_code=500, detail=str(e))
    
    def _generate_dashboard_html(self) -> str:
        """대시보드 HTML 생성"""
        uptime = time.time() - self.performance_metrics["start_time"]
        uptime_str = str(timedelta(seconds=int(uptime)))
        
        active_positions = len(self.risk_manager.active_positions)
        active_monitors = len(self.position_monitor.active_monitors)
        approval_rate = (self.performance_metrics["approved_requests"] / 
                        max(self.performance_metrics["total_requests"], 1)) * 100
        
        total_exposure = sum(p.quantity * p.current_price 
                           for p in self.risk_manager.active_positions.values())
        total_margin = sum(p.margin_required 
                         for p in self.risk_manager.active_positions.values())
        total_pnl = sum(p.unrealized_pnl 
                       for p in self.risk_manager.active_positions.values())
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Phoenix 95 Risk Management Dashboard</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background: #0a0a0a; color: #fff; }}
                .header {{ text-align: center; margin-bottom: 30px; }}
                .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
                .stat-card {{ background: linear-gradient(145deg, #1a1a1a, #2a2a2a); border-radius: 10px; padding: 20px; 
                            border-left: 5px solid #ff6b35; box-shadow: 0 4px 15px rgba(255, 107, 53, 0.1); }}
                .stat-title {{ font-size: 18px; font-weight: bold; margin-bottom: 15px; color: #ff6b35; }}
                .stat-item {{ display: flex; justify-content: space-between; margin: 8px 0; padding: 5px 0; 
                            border-bottom: 1px solid #333; }}
                .stat-value {{ color: #00ff88; font-weight: bold; }}
                .status-indicator {{ display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }}
                .status-healthy {{ background: #00ff88; }}
                .status-warning {{ background: #ffaa00; }}
                .status-danger {{ background: #ff4444; }}
                .footer {{ text-align: center; margin-top: 30px; color: #888; }}
                .risk-level {{ padding: 2px 8px; border-radius: 12px; font-size: 12px; }}
                .risk-low {{ background: #00ff88; color: #000; }}
                .risk-medium {{ background: #ffaa00; color: #000; }}
                .risk-high {{ background: #ff4444; color: #fff; }}
            </style>
            <script>
                setInterval(() => location.reload(), 15000);
            </script>
        </head>
        <body>
            <div class="header">
                <h1>🛡️ Phoenix 95 Risk Management Dashboard</h1>
                <p><span class="status-indicator status-healthy"></span>리스크 시스템 상태: 정상 운영중</p>
                <p>업타임: {uptime_str} | 포트: 8101 | 버전: {RISK_CONFIG["version"]}</p>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-title">📊 포트폴리오 리스크</div>
                    <div class="stat-item">
                        <span>활성 포지션:</span>
                        <span class="stat-value">{active_positions}개</span>
                    </div>
                    <div class="stat-item">
                        <span>총 노출:</span>
                        <span class="stat-value">${total_exposure:,.0f}</span>
                    </div>
                    <div class="stat-item">
                        <span>사용 마진:</span>
                        <span class="stat-value">${total_margin:,.2f}</span>
                    </div>
                    <div class="stat-item">
                        <span>미실현 P&L:</span>
                        <span class="stat-value">${total_pnl:,.2f}</span>
                    </div>
                    <div class="stat-item">
                        <span>마진 사용률:</span>
                        <span class="stat-value">{total_margin/100:.1f}%</span>
                    </div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-title">⚡ 레버리지 설정</div>
                    <div class="stat-item">
                        <span>최대 레버리지:</span>
                        <span class="stat-value">{RISK_CONFIG["max_leverage"]}x</span>
                    </div>
                    <div class="stat-item">
                        <span>마진 모드:</span>
                        <span class="stat-value">{RISK_CONFIG["margin_mode"]}</span>
                    </div>
                    <div class="stat-item">
                        <span>손절율:</span>
                        <span class="stat-value">{RISK_CONFIG["stop_loss_percent"]*100}%</span>
                    </div>
                    <div class="stat-item">
                        <span>익절율:</span>
                        <span class="stat-value">{RISK_CONFIG["take_profit_percent"]*100}%</span>
                    </div>
                    <div class="stat-item">
                        <span>최소 신뢰도:</span>
                        <span class="stat-value">{RISK_CONFIG["confidence_threshold"]*100}%</span>
                    </div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-title">🔍 모니터링 상태</div>
                    <div class="stat-item">
                        <span>활성 모니터:</span>
                        <span class="stat-value">{active_monitors}개</span>
                    </div>
                    <div class="stat-item">
                        <span>체크 간격:</span>
                        <span class="stat-value">{RISK_CONFIG["position_check_interval"]}초</span>
                    </div>
                    <div class="stat-item">
                        <span>리스크 알림:</span>
                        <span class="stat-value">{RISK_CONFIG["risk_alert_threshold"]*100}%</span>
                    </div>
                    <div class="stat-item">
                        <span>긴급 청산:</span>
                        <span class="stat-value">{RISK_CONFIG["emergency_close_threshold"]*100}%</span>
                    </div>
                    <div class="stat-item">
                        <span>메모리 사용률:</span>
                        <span class="stat-value">{psutil.virtual_memory().percent:.1f}%</span>
                    </div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-title">📈 처리 통계</div>
                    <div class="stat-item">
                        <span>총 요청:</span>
                        <span class="stat-value">{self.performance_metrics["total_requests"]:,}</span>
                    </div>
                    <div class="stat-item">
                        <span>승인된 요청:</span>
                        <span class="stat-value">{self.performance_metrics["approved_requests"]:,}</span>
                    </div>
                    <div class="stat-item">
                        <span>거부된 요청:</span>
                        <span class="stat-value">{self.performance_metrics["rejected_requests"]:,}</span>
                    </div>
                    <div class="stat-item">
                        <span>승인률:</span>
                        <span class="stat-value">{approval_rate:.1f}%</span>
                    </div>
                </div>
            </div>
            
            <div class="footer">
                <p>🛡️ Phoenix 95 Risk Management Service v{RISK_CONFIG["version"]}</p>
                <p>헤지펀드급 리스크 관리 • 20x 레버리지 • Kelly Criterion • 실시간 모니터링</p>
                <p>마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
        </body>
        </html>
        """
        
        return html
    
    async def start_background_tasks(self):
        """백그라운드 태스크 시작"""
        logging.info("🔄 백그라운드 태스크 시작")
        
        # 정기 시스템 상태 보고
        status_task = asyncio.create_task(self._periodic_status_report())
        self.background_tasks.append(status_task)
        
        # 메모리 정리 작업
        cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self.background_tasks.append(cleanup_task)
        
        logging.info(f"✅ {len(self.background_tasks)}개 백그라운드 태스크 시작됨")
    
    async def _periodic_status_report(self):
        """정기 상태 보고 (30분마다)"""
        while True:
            try:
                await asyncio.sleep(1800)  # 30분
                
                active_positions = list(self.risk_manager.active_positions.values())
                
                metrics = {
                    "active_positions": len(active_positions),
                    "total_exposure": sum(p.quantity * p.current_price for p in active_positions),
                    "margin_utilization": sum(p.margin_required for p in active_positions) / 10000,
                    "daily_pnl": sum(p.unrealized_pnl for p in active_positions),
                    "memory_usage": psutil.virtual_memory().percent,
                    "signals_processed": self.performance_metrics["total_requests"],
                    "approval_rate": (self.performance_metrics["approved_requests"] / 
                                    max(self.performance_metrics["total_requests"], 1)) * 100
                }
                
                await self.telegram.send_system_status(metrics)
                
            except Exception as e:
                logging.error(f"정기 상태 보고 실패: {e}")
    
    async def _periodic_cleanup(self):
        """정기 정리 작업 (10분마다)"""
        while True:
            try:
                await asyncio.sleep(600)  # 10분
                
                # 메모리 정리
                import gc
                collected = gc.collect()
                
                # 캐시 정리
                current_time = time.time()
                
                # 가격 캐시 정리 (5분 이상 된 것)
                expired_keys = [
                    key for key, timestamp in self.position_monitor.price_cache.items()
                    if key.endswith("_time") and current_time - timestamp > 300
                ]
                
                for key in expired_keys:
                    base_key = key.replace("_time", "")
                    self.position_monitor.price_cache.pop(key, None)
                    self.position_monitor.price_cache.pop(base_key, None)
                
                # 알림 히스토리 정리 (1시간 이상 된 것)
                self.position_monitor.alert_history = deque([
                    alert for alert in self.position_monitor.alert_history
                    if current_time - alert["timestamp"] < 3600
                ], maxlen=1000)
                
                logging.info(f"🧹 정기 정리 완료: {collected}개 객체 수집, {len(expired_keys)}개 캐시 정리")
                
            except Exception as e:
                logging.error(f"정기 정리 실패: {e}")
    
    async def shutdown(self):
        """서비스 종료"""
        logging.info("🛑 Risk Service 종료 시작")
        
        # 모든 포지션 모니터링 중지
        for position_id, task in self.position_monitor.active_monitors.items():
            task.cancel()
            logging.info(f"📊 포지션 모니터링 중지: {position_id}")
        
        # 백그라운드 태스크 중지
        for task in self.background_tasks:
            task.cancel()
        
        logging.info("✅ Risk Service 종료 완료")

# =============================================================================
# 🚀 메인 실행부
# =============================================================================

async def main():
    """메인 실행 함수"""
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('phoenix95_risk_service.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    try:
        # Risk Service 초기화
        risk_service = RiskService()
        
        # 백그라운드 태스크 시작
        await risk_service.start_background_tasks()
        
        # 시그널 핸들러 설정
        def signal_handler(signum, frame):
            logging.info(f"종료 신호 수신: {signum}")
            asyncio.create_task(risk_service.shutdown())
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 시작 메시지
        logging.info("🛡️ Phoenix 95 Risk Management Service 시작")
        logging.info(f"📡 주소: http://{RISK_CONFIG['host']}:{RISK_CONFIG['port']}")
        logging.info(f"🔧 최대 레버리지: {RISK_CONFIG['max_leverage']}x")
        logging.info(f"⚖️ 마진 모드: {RISK_CONFIG['margin_mode']}")
        logging.info(f"📊 손절/익절: ±{RISK_CONFIG['stop_loss_percent']*100}%")
        
        # 서버 실행
        config = uvicorn.Config(
            risk_service.app,
            host=RISK_CONFIG["host"],
            port=RISK_CONFIG["port"],
            log_level="info",
            access_log=True
        )
        
        server = uvicorn.Server(config)
        await server.serve()
        
    except KeyboardInterrupt:
        logging.info("🛑 사용자에 의한 서비스 종료")
    except Exception as e:
        logging.error(f"❌ 서비스 실행 중 오류: {e}\n{traceback.format_exc()}")
    finally:
        logging.info("👋 Phoenix 95 Risk Service 종료")

if __name__ == "__main__":
    asyncio.run(main())
