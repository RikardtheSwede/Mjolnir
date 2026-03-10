# Mjölnir Cadet - IBKR Edition

import sys
import os
import keyboard
import winsound
import json
import math
import time
import asyncio
import logging 

# --- Silence ib_async's internal terminal spam ---
logging.getLogger('ib_async').setLevel(logging.CRITICAL)
from abc import ABC, abstractmethod
from typing import List, Dict, Optional

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QFrame, QComboBox, 
                             QStackedWidget, QProgressBar, QSlider, QCheckBox, QTextBrowser)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QRect, QPoint
from PyQt6.QtGui import QKeySequence, QShortcut, QPainter, QColor, QPen, QFont, QPolygon
from ib_async import *


class MarqueeLabel(QLabel):
    """Special label that automatically scrolls text if it's too long."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._full_text = ""
        self._scroll_pos = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._scroll_tick)
        self._pause_ticks = 0

    def set_custom_text(self, text, color_hex):
        self._full_text = text + "        •        " 
        self._scroll_pos = 0
        self._pause_ticks = 15 
        self.setStyleSheet(f"font-size: 9pt; font-weight: bold; color: {color_hex}; background: transparent; padding-left: 10px;")
        
        if len(text) > 42:
            self.setText(text)
            self._timer.start(100) 
        else:
            self.setText(text)
            self._timer.stop()

    def _scroll_tick(self):
        if self._pause_ticks > 0:
            self._pause_ticks -= 1
            return
        
        self._scroll_pos += 1
        if self._scroll_pos >= len(self._full_text):
            self._scroll_pos = 0
            self._pause_ticks = 15
            
        display_text = self._full_text[self._scroll_pos:] + self._full_text[:self._scroll_pos]
        self.setText(display_text[:42])

# NEW
class TWSInspectorWindow(QWidget):
    """
    NEW: A standalone window to monitor orders detected on the IBKR server.
    Acts as an X-ray for 'The Gentle Sentinel'.
    """
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("TWS INSPECTOR")
        self.setFixedSize(350, 200)
        self.setStyleSheet("background-color: #1a1a1a; color: #dddddd; font-family: Consolas;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        header = QLabel("DETECTED TWS ORDERS")
        header.setStyleSheet("color: #888888; font-weight: bold; border-bottom: 1px solid #333;")
        layout.addWidget(header)
        
        self.order_display = QLabel("Waiting for data...")
        self.order_display.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.order_display.setStyleSheet("background-color: #0d0d0d; padding: 5px; border: 1px solid #333;")
        layout.addWidget(self.order_display, stretch=1)
        
        self.warning_lbl = QLabel("")
        self.warning_lbl.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.warning_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning_lbl.hide()
        layout.addWidget(self.warning_lbl)

    def update_orders(self, orders: list, other_activity: list, has_multiple_sl: bool):
        html = []
        
        # 1. Det valda (låsta) instrumentet
        if not orders:
            html.append("<span style='color: #888;'>No active orders for selected instrument.</span><br><br>")
        else:
            html.append("<span style='color: #00ff00; font-weight: bold;'>ACTIVE INSTRUMENT ORDERS:</span><br>")
            for o in orders:
                html.append(f"<span style='color: #ffffff;'>&nbsp;► {o['action']} {o['type']} {o['qty']} @ {o['price']:.2f}</span><br>")
            html.append("<br>")

        # 2. Övriga instrument på kontot (Bara övervakning)
        if other_activity:
            html.append("<span style='color: #ffaa00; font-weight: bold;'>OTHER INSTRUMENTS (MONITOR ONLY):</span><br>")
            for item in other_activity:
                html.append(f"<span style='color: #aaaaaa;'>&nbsp;• {item['text']}</span><br>")

        if not orders and not other_activity:
            self.order_display.setText("Waiting for data / Account is flat...")
        else:
            self.order_display.setText("".join(html))
            
        if has_multiple_sl:
            self.warning_lbl.setText("⚠ MULTIPLE STOPS DETECTED! Mjölnir disabled.")
            self.warning_lbl.show()
        else:
            self.warning_lbl.hide()

# =============================================================================
# CORE INTERFACES & SIGNALS
# =============================================================================

class ProviderSignals(QObject):
    status_msg = pyqtSignal(str)
    connection_confirmed = pyqtSignal(bool, str)
    connection_lost = pyqtSignal()
    position_update = pyqtSignal(int, float)
    price_update = pyqtSignal(float)
    error_occurred = pyqtSignal(int, str)
    contract_loaded = pyqtSignal(float, float) 
    order_filled = pyqtSignal(str, int, str, float)

class ExecutionProvider(ABC):
    def __init__(self):
        self.signals = ProviderSignals()
    @abstractmethod
    def connect(self, settings: dict) -> bool: pass
    @abstractmethod
    def disconnect(self): pass
    @abstractmethod
    def is_connected(self) -> bool: pass
    @abstractmethod
    def set_contract(self, symbol: str, exchange: str): pass
    @abstractmethod
    def cancel_order(self, order_ref: str): pass
    @abstractmethod
    def place_bracket(self, action: str, qty: int, lmt_price: float, tp_price: float, sl_price: float): pass
    @abstractmethod
    def place_single_order(self, action: str, qty: int, price: float, order_ref: str): pass
    @abstractmethod
    def cancel_all(self): pass
    @abstractmethod
    def modify_order(self, order_ref: str, new_price: float, new_qty: Optional[int] = None): pass
    @abstractmethod
    def cancel_order_by_id(self, order_id: int): pass # NY RAD
    @abstractmethod
    def get_order_price(self, order_ref: str) -> Optional[float]: pass
    @abstractmethod
    def get_active_order_count(self) -> int: pass

# =============================================================================
# IBKR IMPLEMENTATION
# =============================================================================

class IBKRProvider(ExecutionProvider):
    def __init__(self):
        super().__init__()
        self.ib = IB()
        self.contract: Optional[Contract] = None
        self.mkt_data = None
        self._setup_events()

    def on_error(self, reqId, errorCode, errorString, contract):
        ignore_codes = [201, 202, 2103, 2104, 2105, 2106, 2107, 2108, 2109, 2119, 2158, 10349, 10148]
        if errorCode not in ignore_codes:
            self.signals.error_occurred.emit(errorCode, errorString)

    def _setup_events(self):
        self.ib.errorEvent += self.on_error
        self.ib.positionEvent += self.on_position
        self.ib.pendingTickersEvent += self.on_ticker_update
        self.ib.execDetailsEvent += self.on_exec_details
        self.ib.disconnectedEvent += self.on_disconnect

    def connect(self, settings: dict) -> bool:
        port = settings.get('port', 7497)
        is_paper = (port in [7497, 4002])
        try:
            if self.ib.isConnected(): self.disconnect()
            self.ib = IB()
            self._setup_events()
            self.ib.connect('127.0.0.1', port, clientId=0)
            self.ib.client.reqAutoOpenOrders(True)
            
            attempts = 0
            while not self.ib.managedAccounts() and attempts < 15:
                self.ib.sleep(0.1)
                attempts += 1
            
            accounts = self.ib.managedAccounts()
            if not accounts: return False
                
            main_account = accounts[0]
            if (is_paper and not main_account.startswith('D')) or \
               (not is_paper and not main_account.startswith('U')):
                self.signals.status_msg.emit("CONNECTION REJECTED: Account mismatch.")
                self.disconnect()
                return False
                
            self.signals.connection_confirmed.emit(True, main_account)
            self.signals.status_msg.emit(f"Secure connection: {main_account}")
            return True
        except Exception as e:
            self.signals.connection_confirmed.emit(False, str(e))
            return False
        
    def on_disconnect(self):
        self.contract = None
        self.mkt_data = None
        self.signals.connection_lost.emit()

    def disconnect(self): 
        try:
            if self.mkt_data: self.ib.cancelMktData(self.mkt_data.contract)
            self.ib.disconnect()
            self.contract = None
            self.mkt_data = None
        except: pass

    def is_connected(self) -> bool: 
        return self.ib.isConnected()

    def clear_contract(self):
        if self.mkt_data: self.ib.cancelMktData(self.mkt_data.contract)
        self.contract = None
        self.mkt_data = None
        self.signals.price_update.emit(0.0)

    def set_contract(self, symbol: str, exchange: str):
        if not self.is_connected(): return
        base_contract = Future(symbol=symbol, exchange=exchange)
        details = self.ib.reqContractDetails(base_contract)
        if not details: 
            self.signals.status_msg.emit(f"ERROR: Contract {symbol} not found.")
            return
        details = sorted(details, key=lambda x: x.contract.lastTradeDateOrContractMonth)[0]
        self.contract = details.contract
        self.ib.qualifyContracts(self.contract)
        self.signals.contract_loaded.emit(details.minTick, float(self.contract.multiplier or 1.0))
        
        if self.mkt_data: self.ib.cancelMktData(self.mkt_data.contract)
        self.mkt_data = self.ib.reqMktData(self.contract, '', False, False)
        self.ib.reqAllOpenOrders()
        for pos in self.ib.positions():
            if pos.contract.conId == self.contract.conId: self.on_position(pos)
        self.signals.status_msg.emit(f"READY: {self.contract.localSymbol}")

    def place_bracket(self, action: str, qty: int, lmt_price: float, tp_price: float, sl_price: float):
        if not self.contract or not self.is_connected(): return
        bracket = self.ib.bracketOrder(action, qty, lmt_price, lmt_price, sl_price)
        entry, sl = bracket[0], bracket[2]
        entry.orderRef, sl.orderRef = "ENTRY", "SL"
        entry.tif = sl.tif = 'GTC'
        entry.outsideRth = sl.outsideRth = True
        entry.usePriceMgmtAlgo = True
        entry.transmit = False
        sl.transmit = True 
        self.ib.placeOrder(self.contract, entry)
        self.ib.placeOrder(self.contract, sl)
        self.signals.status_msg.emit(f"SENT: {action} {qty} (Bracket Active)")

    def place_single_order(self, action: str, qty: int, price: float, order_ref: str):
        if not self.contract or not self.is_connected(): return
        order = LimitOrder(action, qty, price, outsideRth=True, tif='GTC')
        order.orderRef = order_ref
        order.usePriceMgmtAlgo = True
        order.transmit = True  
        trade = self.ib.placeOrder(self.contract, order)
        if order_ref == "SCALE":
            QTimer.singleShot(10000, lambda: self.cleanup_stale_order(trade))

    def cleanup_stale_order(self, trade):
        if self.is_connected() and trade.orderStatus.status not in OrderStatus.DoneStates:
            self.ib.cancelOrder(trade.order)

    # REPLACE (I IBKRProvider)
    def cancel_all(self):
        if not self.contract or not self.is_connected(): return
        count = 0
        for t in self.ib.openTrades():
            if t.contract.conId == self.contract.conId:
                # Vi ignorerar ordrar som redan är klara eller håller på att avbrytas ('PendingCancel')
                if t.orderStatus.status not in ['Cancelled', 'Filled', 'Inactive', 'ApiCancelled', 'PendingCancel']:
                    self.ib.cancelOrder(t.order)
                    count += 1
                    
        if count > 0: self.signals.status_msg.emit(f"CLEANUP: {count} active/pending orders erased.")


    def get_active_order_count(self) -> int:
        if not self.contract or not self.is_connected(): return 0
        return len([t for t in self.ib.openTrades() if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates])

    # REPLACE
    def get_order_price(self, order_ref: str) -> float:
        if not self.is_connected() or not self.contract: return 0.0
        
        pos = next((p for p in self.ib.positions() if p.contract.conId == self.contract.conId), None)
        pos_qty = pos.position if pos else 0
        
        target_action = None
        if pos_qty > 0:
            target_action = "SELL"
        elif pos_qty < 0:
            target_action = "BUY"
        else:
            # Om flat: Leta efter Pending Anchor för att veta vilken riktning SL ligger på
            for t in self.ib.openTrades():
                if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                    if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                        target_action = "SELL" if t.order.action == "BUY" else "BUY"
                        break
            
        for t in self.ib.openTrades():
            if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                
                # 1. Mjölnir Native Match
                if t.order.orderRef == order_ref:
                    return getattr(t.order, 'auxPrice', getattr(t.order, 'lmtPrice', 0.0))
                
                # 2. Gentle Sentinel Match (Kräver nu bara att target_action hittats)
                if target_action and t.order.action == target_action:
                    if order_ref == 'SL' and t.order.orderType in ['STP', 'STP LMT', 'TRAIL']:
                        return t.order.auxPrice
                    # Säkerställ att vi inte råkar läsa en Limit Entry Order av misstag
                    elif order_ref == 'TP' and t.order.orderType == 'LMT' and t.order.parentId != 0:
                        return t.order.lmtPrice
                        
        return 0.0
    
    # REPLACE
    def modify_order(self, order_ref: str, new_price: float, new_qty: Optional[int] = None):
        if not self.is_connected() or not self.contract: return
        
        pos = next((p for p in self.ib.positions() if p.contract.conId == self.contract.conId), None)
        pos_qty = pos.position if pos else 0
        
        target_action = None
        if pos_qty > 0:
            target_action = "SELL"
        elif pos_qty < 0:
            target_action = "BUY"
        else:
            # Om flat: Leta efter Pending Anchor
            for t in self.ib.openTrades():
                if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                    if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                        target_action = "SELL" if t.order.action == "BUY" else "BUY"
                        break

        for t in self.ib.openTrades():
            if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                is_match = False
                
                if t.order.orderRef == order_ref:
                    is_match = True
                elif target_action and t.order.action == target_action:
                    if order_ref == 'SL' and t.order.orderType in ['STP', 'STP LMT', 'TRAIL']:
                        is_match = True
                    elif order_ref == 'TP' and t.order.orderType == 'LMT' and t.order.parentId != 0:
                        is_match = True

                if is_match:
                    if order_ref == 'SL':
                        # THE DUAL-MOVE LOGIC: Flytta Limit-priset med exakt samma avstånd!
                        if t.order.orderType == 'STP LMT':
                            price_diff = new_price - t.order.auxPrice
                            t.order.lmtPrice = round(t.order.lmtPrice + price_diff, 4)
                        # Sätt sedan det nya Stop-priset
                        t.order.auxPrice = new_price
                        
                    elif order_ref == 'TP':
                        t.order.lmtPrice = new_price
                        
                    if new_qty is not None:
                        t.order.totalQuantity = new_qty
                        
                    self.ib.placeOrder(t.contract, t.order)
                    return # Avbryt efter att vi hittat och modifierat rätt order


    def cancel_order_by_id(self, order_id: int):
        if not self.is_connected(): return
        for t in self.ib.openTrades():
            if t.order.orderId == order_id and t.orderStatus.status not in OrderStatus.DoneStates:
                self.ib.cancelOrder(t.order)
                return  

    def cancel_order(self, order_ref: str):
        if not self.is_connected(): return
        for t in self.ib.openTrades():
            if self.contract and t.contract.conId == self.contract.conId:
                if t.order.orderRef == order_ref and t.orderStatus.status not in OrderStatus.DoneStates:
                    self.ib.cancelOrder(t.order)

    def on_exec_details(self, trade, fill):
        if self.contract and trade.contract.conId == self.contract.conId:
            self.signals.order_filled.emit(trade.order.orderRef, int(fill.execution.shares), fill.execution.side, fill.execution.price)

    def on_position(self, position):
        if self.contract and position.contract.conId == self.contract.conId:
            mult = float(self.contract.multiplier or 1.0)
            self.signals.position_update.emit(int(position.position), position.avgCost / mult)

    def on_ticker_update(self, tickers):
        if self.mkt_data in tickers:
            p = self.mkt_data.last if not math.isnan(self.mkt_data.last) else self.mkt_data.close
            if p and not math.isnan(p): self.signals.price_update.emit(p)

# =============================================================================
# MODIFIED: SENTINEL MANAGER (THE CADET LOGIC)
# =============================================================================

class SentinelManager(QObject):
    log_signal = pyqtSignal(str)
    ui_update = pyqtSignal(dict)
    connection_status = pyqtSignal(bool, str)
    connection_lost_signal = pyqtSignal()
    flash_signal = pyqtSignal(str)
    sl_reject_signal = pyqtSignal()
    arm_reject_signal = pyqtSignal()      # NYTT: Signal för oarmerad trade
    cooldown_reject_signal = pyqtSignal() # NYTT: Signal för trade under cooldown

    def __init__(self):
        super().__init__()
        self.providers: List[ExecutionProvider] = []
        self.is_armed = False
        self.cooldown = False
        self.last_known_sl = 0.0 
        
        self.post_trade_cooldown_active = False
        self.cooldown_remaining = 0
        self.cooldown_total = 10
        self.pt_timer = QTimer()
        self.pt_timer.timeout.connect(self._tick_post_trade_cooldown)
        
        # SL Grace Period
        self.sl_locked = False
        self.grace_time_remaining = 0
        self.grace_timer = QTimer()
        self.grace_timer.timeout.connect(self._tick_grace_period)

        self.pos_qty, self.avg_price, self.current_price = 0, 0.0, 0.0
        self._tracked_pos = 0
        self._tracked_cost = 0.0
        self.pure_avg_price = 0.0
        
        self.entry_time = 0.0
        self.min_tick = 0.25
        self.trade_qty, self.tp_points, self.sl_points, self.slippage = 1, 10.0, 5.0, 2.0
        self.max_qty = 3
        
        self.be_offset = 1.0
        self.trail_active = False
        self.turbo_mode = False
        self.peak_price = 0.0
        self.virtual_tp = 0.0
        self.use_virtual_tp = False
        
        self.trail_points = 10.0
        self.tight_trail_points = 3.0
        self.current_trail_distance = 10.0

        self.pending_nudges: Dict[str, float] = {}
        self.nudge_timer = QTimer()
        self.nudge_timer.setSingleShot(True)
        self.nudge_timer.timeout.connect(self.commit_nudges)

    def _start_grace_period(self):
        self.sl_locked = False
        self.grace_time_remaining = 200  # 20 sekunder (100ms ticks = 200 ticks)
        self.grace_timer.start(100)
        self.log_signal.emit("CADET: 20s SL Grace Period started. 🔓")
        self.update_ui_state()

    def _tick_grace_period(self):
        self.grace_time_remaining -= 1
        if self.grace_time_remaining <= 0:
            self.grace_timer.stop()
            self.sl_locked = True
            self.log_signal.emit("CADET: Grace Period expired. SL Direction Locked. 🔒")
        self.update_ui_state()

    def add_provider(self, provider: ExecutionProvider):
        provider.signals.status_msg.connect(self.log_signal.emit)
        provider.signals.connection_confirmed.connect(self.connection_status.emit)
        provider.signals.connection_lost.connect(self.connection_lost_signal.emit)
        provider.signals.position_update.connect(self.handle_position)
        provider.signals.price_update.connect(self.handle_price)
        provider.signals.error_occurred.connect(self.handle_error)
        provider.signals.contract_loaded.connect(self.handle_contract_info)
        provider.signals.order_filled.connect(self.handle_order_fill)
        self.providers.append(provider)

    def handle_contract_info(self, min_tick, multiplier):
        self.min_tick = min_tick

    def handle_position(self, q, a):
        if self.pos_qty != 0 and q == 0:
            if self.is_armed:
                self._start_post_trade_cooldown(10)
                for p in self.providers:
                    if p.is_connected(): p.cancel_all()
            
        if q != 0 and self._tracked_pos == 0:
            self._tracked_pos = q
            self._tracked_cost = abs(q) * a
            self.pure_avg_price = a
            
        if q == 0:
            self._tracked_pos = 0
            self._tracked_cost = 0.0
            self.pure_avg_price = 0.0
            self.auto_be_active = False
            self.trail_active = False
            self.turbo_mode = False
            self.virtual_tp = 0.0
            
            # NYTT: Stäng av Grace Period och lås upp
            self.grace_timer.stop()
            self.sl_locked = False
            
        self.avg_price = self.pure_avg_price if self.pure_avg_price > 0 else a
            
        if self.pos_qty == 0 and q != 0:
            self.entry_time = time.time()
            self.trail_active = False 
            self.turbo_mode = False
            self.peak_price = self.current_price
            self.current_trail_distance = self.trail_points
            
            self._start_grace_period() # NYTT: Starta vår 20s grace-timer!
            
            QTimer.singleShot(200, lambda: self._perform_auto_snap(q))
            
        self.pos_qty = q
        self.update_ui_state()

    def _perform_auto_snap(self, q):
        # NYTT: Avbryt omedelbart om Mjölnir är i SAFE-mode!
        if not self.is_armed or self.pos_qty == 0: return 
        
        direction = 1 if q > 0 else -1
        anchor = self.pure_avg_price if self.pure_avg_price > 0 else self.avg_price
        
        exact_sl = round(round((anchor - (self.sl_points * direction)) / self.min_tick) * self.min_tick, 4)
        exact_tp = round(round((anchor + (self.tp_points * direction)) / self.min_tick) * self.min_tick, 4)
        self.virtual_tp = exact_tp

        for p in self.providers:
            if p.is_connected(): p.modify_order('SL', exact_sl)
        self.log_signal.emit(f"CADET: SL Locked to pure fill ({exact_sl})")
        

    def update_ui_state(self):
        open_orders = 0
        order_details = []
        other_details = []

        target_action = None
        pending_anchor = 0.0
        pending_direction = 0

        # 1. Bestäm riktning och hitta eventuell väntande Entry (Anchor)
        if self.pos_qty > 0: 
            target_action = "SELL"
        elif self.pos_qty < 0: 
            target_action = "BUY"
        else:
            # Vi är flat. Leta efter en väntande Limit-order i TWS.
            for p in self.providers:
                if p.is_connected() and p.contract:
                    for t in p.ib.openTrades():
                        if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                            if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                                pending_anchor = getattr(t.order, 'lmtPrice', getattr(t.order, 'auxPrice', 0.0))
                                pending_direction = 1 if t.order.action == "BUY" else -1
                                target_action = "SELL" if pending_direction == 1 else "BUY"
                                break

        active_stops = []

        # 2. Samla in all order- och positionsdata
        for p in self.providers:
            if p.is_connected():
                for pos in p.ib.positions():
                    if pos.position == 0: continue
                    is_active = (p.contract and pos.contract.conId == p.contract.conId)
                    if not is_active:
                        pos_str = f"POS: {pos.contract.symbol} {'LONG' if pos.position > 0 else 'SHORT'} {abs(pos.position)} @ {pos.avgCost:.2f}"
                        other_details.append({'type': 'position', 'text': pos_str})

                for t in p.ib.openTrades():
                    if t.orderStatus.status in OrderStatus.DoneStates or t.orderStatus.status == 'PendingCancel': 
                        continue

                    is_active = (p.contract and t.contract.conId == p.contract.conId)
                    price = getattr(t.order, 'auxPrice', 0.0) if t.order.orderType in ['STP', 'STP LMT', 'TRAIL'] else getattr(t.order, 'lmtPrice', 0.0)

                    if is_active:
                        open_orders += 1
                        order_details.append({
                            'action': t.order.action,
                            'type': t.order.orderType,
                            'qty': int(t.order.totalQuantity),
                            'price': price
                        })

                        if target_action and t.order.action == target_action and t.order.orderType in ['STP', 'STP LMT', 'TRAIL']:
                            active_stops.append({'id': t.order.orderId, 'qty': int(t.order.totalQuantity), 'price': price, 'ref': t.order.orderRef})
                    else:
                        ord_str = f"ORD: {t.contract.symbol} {t.order.action} {t.order.orderType} {int(t.order.totalQuantity)} @ {price:.2f}"
                        other_details.append({'type': 'order', 'text': ord_str})

        # ==========================================
        # THE MAGNETIC BRACKET & QTY-SYNC
        # ==========================================
        has_multiple_sl = False
        expected_sl = 0.0 

        if pending_anchor > 0.0:
            expected_sl = round(round((pending_anchor - (self.sl_points * pending_direction)) / self.min_tick) * self.min_tick, 4)

        if self.is_armed and len(active_stops) > 0:
            master_sl = active_stops[0]

            # A. MAGNETIC BRACKET (Innan fyllning)
            if self.pos_qty == 0 and pending_anchor > 0.0:
                if getattr(self, '_last_pending_anchor', 0.0) != pending_anchor:
                    self._last_pending_anchor = pending_anchor

                    if abs(master_sl['price'] - expected_sl) > (self.min_tick * 0.1):
                        for p in self.providers:
                            if p.is_connected():
                                p.modify_order('SL', expected_sl)
                                self.log_signal.emit(f"MAGNETIC: Snapped pending SL to {self.sl_points:.2f} pts.")

            # B. QTY-SYNC (Efter fyllning)
            elif self.pos_qty != 0:
                self._last_pending_anchor = 0.0 
                if master_sl['qty'] != abs(self.pos_qty):
                    for p in self.providers:
                        if p.is_connected():
                            p.modify_order('SL', master_sl['price'], new_qty=abs(self.pos_qty))
                            self.log_signal.emit(f"SYNC: Adjusted Master SL to {abs(self.pos_qty)} contracts.")

            # C. STÄDPATRULLEN (Auto-Merge)
            if len(active_stops) > 1:
                has_multiple_sl = True
                for extra_sl in active_stops[1:]:
                    for p in self.providers:
                        if p.is_connected() and hasattr(p, 'cancel_order_by_id'):
                            p.cancel_order_by_id(extra_sl['id'])
                            self.log_signal.emit(f"MERGE: Cancelled overlapping SL order.")
        else:
            self._last_pending_anchor = 0.0

        data = {
            'pos': int(self.pos_qty), 'avg': self.avg_price, 'price': self.current_price,
            'pl': 0.0, 'tp_pts': self.tp_points, 'sl_pts': self.sl_points, 
            'is_armed': self.is_armed, 'open_orders': open_orders,
            'trail_active': self.trail_active, 'turbo_mode': self.turbo_mode,
            'pt_cooldown': getattr(self, 'post_trade_cooldown_active', False),
            'pt_remaining': getattr(self, 'cooldown_remaining', 0),
            'pt_total': getattr(self, 'cooldown_total', 10),
            'display_direction': 1,
            'tws_orders': order_details,
            'other_activity': other_details, 
            'multi_sl_warning': has_multiple_sl,
            'pending_entry': pending_anchor, 
            'pending_sl': expected_sl,
            # NYTT: Data för UI-uppdateringar
            'sl_locked': getattr(self, 'sl_locked', False),
            'grace_remaining': getattr(self, 'grace_time_remaining', 0)
        }

        if self.pos_qty != 0:
            direction = 1 if self.pos_qty > 0 else -1
            data['display_direction'] = direction 
            data['pl'] = (self.current_price - self.avg_price) * direction

        self.ui_update.emit(data)

    def handle_price(self, p):
        self.current_price = p
        if self.pos_qty == 0: 
            self.update_ui_state()
            return
            
        direction = 1 if self.pos_qty > 0 else -1
        if self.peak_price == 0.0: self.peak_price = p
        else: self.peak_price = max(self.peak_price, p) if direction == 1 else min(self.peak_price, p)

        # NYTT: VIRTUAL TP LOGIK
        if self.use_virtual_tp and self.virtual_tp > 0.0 and not self.turbo_mode:
            if (direction == 1 and p >= self.virtual_tp) or (direction == -1 and p <= self.virtual_tp):
                self.log_signal.emit(f"🎯 VIRTUAL TP HIT ({self.virtual_tp:.2f}): Activating Turbo Trail!")
                self.trail_active = True
                self.turbo_mode = True
                self.current_trail_distance = self.tight_trail_points
                self.process_trailing_stop()

        if self.trail_active: self.process_trailing_stop()
        self.update_ui_state()

    def handle_order_fill(self, ref, qty, side, price):
        self.log_signal.emit(f"⚡ FILL: {side} {qty} @ {price:.2f} [{ref}]")
        side_mult = 1 if side == "BOT" else -1
        signed_qty = qty * side_mult
        new_pos = self._tracked_pos + signed_qty
        
        if new_pos == 0:
            self._tracked_cost = 0.0
            self.pure_avg_price = 0.0
        elif (self._tracked_pos > 0 and side_mult > 0) or (self._tracked_pos < 0 and side_mult < 0):
            self._tracked_cost += (qty * price)
            self.pure_avg_price = self._tracked_cost / abs(new_pos)
        else:
            if self._tracked_pos != 0:
                self._tracked_cost -= (qty * (self._tracked_cost / abs(self._tracked_pos)))
            self.pure_avg_price = (self._tracked_cost / abs(new_pos)) if new_pos != 0 else 0.0
        self._tracked_pos = new_pos
        
        if ref == "SCALE":
            for p in self.providers:
                if p.is_connected():
                    for target_ref in ['TP', 'SL']:
                        current_p = p.get_order_price(target_ref)
                        if current_p: p.modify_order(target_ref, current_p, new_qty=abs(self.pos_qty))


    # REPLACE
    def process_trailing_stop(self):
        direction = 1 if self.pos_qty > 0 else -1
        target_sl = round(round((self.peak_price - (self.current_trail_distance * direction)) / self.min_tick) * self.min_tick, 4)
        
        for p in self.providers:
            if p.is_connected():
                # Kolla om vi redan har ett pending target i kön som är bättre, annars hämta från IBKR
                current_sl = self.pending_nudges.get('SL', p.get_order_price('SL'))
                
                if current_sl and ((direction == 1 and target_sl > current_sl) or (direction == -1 and target_sl < current_sl)):
                    self.pending_nudges['SL'] = target_sl
                    self.nudge_timer.start(400) # Skjuter upp API-anropet för att förhindra spam

    def handle_error(self, c, m):
        self.log_signal.emit(f"API ERROR [{c}]: {m}")

    def is_connected(self) -> bool:
        return any(p.is_connected() for p in self.providers)

    def clear_instrument(self):
        self.current_price = 0.0
        for p in self.providers:
            if hasattr(p, 'clear_contract'): p.clear_contract()
        self.update_ui_state()

    def execute_trade(self, action: str):
        # 1. GUARD RAIL: Oarmerat system
        if not self.is_armed:
            self.arm_reject_signal.emit()
            self.log_signal.emit("REJECTED: System is SAFE. Arm first.")
            return
            
        # 2. GUARD RAIL: Cooldown aktiv
        if getattr(self, 'post_trade_cooldown_active', False):
            self.cooldown_reject_signal.emit()
            self.log_signal.emit("REJECTED: Post-trade cooldown active. ⏳")
            return

        if self.cooldown: return

        self.cooldown = True
        QTimer.singleShot(400, lambda: setattr(self, 'cooldown', False))
        
        qty = self.trade_qty
        side = 1 if action == "BUY" else -1
        
        # THE AGGRESSIVE ENTRY TICK FIX (Minst 2 ticks marginal)
        slip_ticks = max(2, math.ceil(self.slippage / self.min_tick))
        lmt = round(self.current_price + (slip_ticks * self.min_tick * side), 4)
        
        is_scaling = False
        if (self.pos_qty > 0 and action == "BUY") or (self.pos_qty < 0 and action == "SELL"):
            if abs(self.pos_qty) + qty > self.max_qty:
                self.log_signal.emit(f"REJECTED: Max Qty ({self.max_qty}) reached.")
                return
            is_scaling = True
        elif (self.pos_qty > 0 and action == "SELL") or (self.pos_qty < 0 and action == "BUY"):
            if abs(self.pos_qty) <= qty:
                self.execute_close()
                return
            is_scaling = True

        if is_scaling:
            for p in self.providers:
                if p.is_connected(): p.place_single_order(action, qty, lmt, "SCALE")
        else:
            sl_price = round(round((self.current_price - (self.sl_points * side)) / self.min_tick) * self.min_tick, 4)
            for p in self.providers:
                if p.is_connected(): p.place_bracket(action, qty, lmt, 0.0, sl_price)

    def execute_be_move(self):
        if self.pos_qty == 0: return
        direction = 1 if self.pos_qty > 0 else -1
        target_price = round(round((self.avg_price + (self.be_offset * direction)) / self.min_tick) * self.min_tick, 4)
        for p in self.providers:
            if p.is_connected(): p.modify_order('SL', target_price)
        self.log_signal.emit(f"MANUAL BE: Protection at {target_price}")
    
    def escalate_trail(self):
        if self.pos_qty == 0: return
        if not self.trail_active:
            self.trail_active, self.turbo_mode = True, False
            self.peak_price, self.current_trail_distance = self.current_price, self.trail_points
            self.log_signal.emit(f"TRAIL ACTIVE: {self.trail_points} pts.")
        elif not self.turbo_mode:
            self.turbo_mode, self.current_trail_distance = True, self.tight_trail_points
            self.log_signal.emit(f"TURBO ACTIVE: {self.tight_trail_points} pts.")
            self.process_trailing_stop()
        self.update_ui_state()

    def nudge_order(self, order_type: str, price_ticks: int):
        # NYTT: GUARD RAIL FÖR SL RETREAT
        if order_type == 'SL' and self.pos_qty != 0:
            if price_ticks < 0: # Försöker flytta SL BORT från priset (öka risken)
                if self.sl_locked:
                    self.sl_reject_signal.emit() # Avbryt och signalera till UI
                    self.log_signal.emit("GUARD RAIL: SL Retreat BLOCKED. 🔒")
                    return
            elif price_ticks > 0: # Försöker flytta SL NÄRMARE priset (minska risken)
                if not self.sl_locked:
                    self.sl_locked = True
                    self.grace_timer.stop()
                    self.grace_time_remaining = 0
                    self.log_signal.emit("CADET: Risk reduced. SL Direction Locked early. 🔒")

        # 1. UPPDATERA MJÖLNIRS INTERNA MINNE
        if order_type == 'SL':
            self.sl_points = max(self.min_tick, self.sl_points - (price_ticks * self.min_tick))
        elif order_type == 'TP':
            self.tp_points = max(self.min_tick, self.tp_points + (price_ticks * self.min_tick))

        self.update_ui_state()
        self._pending_log_type = order_type 

        # 2. FLYTTA FYSISK ORDER (Endast om armerad)
        if self.is_armed:
            anchor = 0.0
            direction = 1

            if self.pos_qty != 0:
                anchor = self.avg_price
                direction = 1 if self.pos_qty > 0 else -1
            else:
                for p in self.providers:
                    if p.is_connected() and p.contract:
                        for t in p.ib.openTrades():
                            if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                                if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                                    anchor = getattr(t.order, 'lmtPrice', getattr(t.order, 'auxPrice', 0.0))
                                    direction = 1 if t.order.action == "BUY" else -1
                                    break

            if anchor > 0.0:
                exact_price = 0.0
                if order_type == 'SL':
                    exact_price = round(round((anchor - (self.sl_points * direction)) / self.min_tick) * self.min_tick, 4)
                elif order_type == 'TP':
                    exact_price = round(round((anchor + (self.tp_points * direction)) / self.min_tick) * self.min_tick, 4)

                if exact_price > 0.0:
                    self.pending_nudges[order_type] = exact_price

        self.nudge_timer.start(400)

    def commit_nudges(self):
        # 1. Utför API-anrop om vi är live
        for ref, price in self.pending_nudges.items():
            for p in self.providers:
                if p.is_connected(): p.modify_order(ref, price)
            self.log_signal.emit(f"API: Transmitted {ref} order update ({price:.2f})")
            
        self.pending_nudges.clear()

        # 2. Skriv ut den debouncade UI-loggen (visas oavsett om vi är i SAFE eller ARMED)
        if hasattr(self, '_pending_log_type') and self._pending_log_type:
            ref = self._pending_log_type
            pts = self.sl_points if ref == 'SL' else self.tp_points
            self.log_signal.emit(f"CADET: {ref} Profile locked at {pts:.2f} pts")
            self._pending_log_type = None
            

    def execute_close(self):
        for p in self.providers:
            if p.is_connected(): 
                p.cancel_all()
                if self.pos_qty != 0:
                    action = "SELL" if self.pos_qty > 0 else "BUY"
                    side = 1 if action == "BUY" else -1
                    
                    # THE PANIC BUTTON TICK FIX (Minst 4 ticks aggressiv marginal!)
                    slip_ticks = max(4, math.ceil(self.slippage / self.min_tick))
                    lmt = round(self.current_price + (slip_ticks * self.min_tick * side), 4)
                    
                    self.log_signal.emit(f"API: Executing Marketable Close ({action} @ {lmt:.2f})")
                    p.place_single_order(action, abs(self.pos_qty), lmt, "CLOSE")

    def _start_post_trade_cooldown(self, seconds: int):
        self.post_trade_cooldown_active, self.cooldown_total = True, seconds
        self.cooldown_remaining = seconds
        self.pt_timer.start(1000)
        self.update_ui_state()

    def _tick_post_trade_cooldown(self):
        self.cooldown_remaining -= 1
        if self.cooldown_remaining <= 0:
            self.pt_timer.stop()
            self.post_trade_cooldown_active = False
        self.update_ui_state()


# NEW: The Thread-Safe Bridge for Global Hotkeys
class GlobalHotkeyManager(QObject):
    sig_arm = pyqtSignal()
    sig_trade = pyqtSignal(str)
    sig_close = pyqtSignal()
    sig_trail = pyqtSignal()
    sig_be = pyqtSignal()
    sig_nudge = pyqtSignal(str, int)

    def __init__(self, gui):
        super().__init__()
        self.gui = gui
        self.manager = gui.manager
        
        # 1. Koppla signalerna (Dessa skickas nu 100% säkert till huvud-GUI-tråden)
        self.sig_arm.connect(self.gui.btn_arm.click)
        self.sig_trade.connect(self.manager.execute_trade)
        self.sig_close.connect(self.manager.execute_close)
        self.sig_trail.connect(self.manager.escalate_trail)
        self.sig_be.connect(self.manager.execute_be_move)
        self.sig_nudge.connect(self.manager.nudge_order)
        
        # 2. Sätt upp de globala lyssnarna som skickar signalerna
        keyboard.add_hotkey('ctrl+shift+a', self.sig_arm.emit)
        keyboard.add_hotkey('ctrl+shift+b', lambda: self.sig_trade.emit("BUY"))
        keyboard.add_hotkey('ctrl+shift+s', lambda: self.sig_trade.emit("SELL"))
        keyboard.add_hotkey('ctrl+shift+c', self.sig_close.emit)
        keyboard.add_hotkey('ctrl+shift+t', self.sig_trail.emit)
        keyboard.add_hotkey('ctrl+shift+e', self.sig_be.emit)
        
        # RISK MANAGEMENT HOTKEYS (+ / -)
        # ctrl+shift++ ökar risken (SL flyttas bort från priset)
        keyboard.add_hotkey('ctrl+shift+I', lambda: self.sig_nudge.emit('SL', -1))
        # ctrl+shift+- minskar risken (SL flyttas närmare priset)
        keyboard.add_hotkey('ctrl+shift+K', lambda: self.sig_nudge.emit('SL', 1))
        

# =============================================================================
# MODIFIED: GUI (MJÖLNIR - THE CADET)
# =============================================================================

class MjolnirGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.log_messages, self.instruments = [], {}
        self.pending_selection, self.active_instrument_name = None, ""
        self.theme_color = "#444444"
        self.manager = SentinelManager()
        self.ib_provider = IBKRProvider()
        self.manager.add_provider(self.ib_provider)
        
        self.init_ui()
        self.load_instruments()
        self.load_settings()
        
        self.alarm_timer = QTimer()
        self.alarm_timer.timeout.connect(self.blink_connection_alarm)
        self.emergency_timer = QTimer()
        self.emergency_timer.timeout.connect(self.blink_emergency_ui)
        self.setup_connections()
        self.setup_hotkeys()

    def init_ui(self):
        self.setWindowTitle("MJÖLNIR - THE CADET")
        self.expanded_width, self.collapsed_width = 720, 380 
        self.setFixedSize(self.expanded_width, 700) 
        self.setStyleSheet("background-color: #1e1e1e; color: #ffffff;")
        
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20) 

        # --- LEFT COLUMN (SETUP) ---
        self.left_panel = QFrame()
        self.left_panel.setFixedWidth(240) 
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0); left_layout.setSpacing(10)
        
        lbl_setup = QLabel("MJÖLNIR SETUP")
        lbl_setup.setStyleSheet("color: #666; font-weight: bold; font-size: 10pt;")
        left_layout.addWidget(lbl_setup)
        
        conn_box = QHBoxLayout()
        self.combo_env = QComboBox()
        self.combo_env.setFixedHeight(35)
        self.combo_env.addItems(["TWS PAPER (7497)", "TWS LIVE (7496)", "────────────────────", "GATEWAY PAPER (4002)", "GATEWAY LIVE (4001)"])
        self.combo_env.setStyleSheet("QComboBox { background-color: #333333; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #555555; } QComboBox::drop-down { border: none; width: 25px; }")
        
        self.btn_connect = QPushButton("🔗")
        self.btn_connect.setFixedSize(45, 35)
        self.btn_connect.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")
        self.btn_connect.clicked.connect(self.do_connect)
        conn_box.addWidget(self.combo_env, stretch=1); conn_box.addWidget(self.btn_connect)
        left_layout.addLayout(conn_box)

        inst_layout = QHBoxLayout()
        self.combo_symbol = QComboBox()
        self.combo_symbol.setFixedHeight(35); self.combo_symbol.setEnabled(False)
        self.combo_symbol.setStyleSheet("QComboBox { background-color: #222222; color: #666666; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #333333; } QComboBox::drop-down { border: none; width: 25px; }")
        self.combo_symbol.currentTextChanged.connect(self.on_instrument_selected)
        
        self.btn_lock = QPushButton("🔒")
        self.btn_lock.setFixedSize(45, 35)
        self.btn_lock.setEnabled(False)
        self.btn_lock.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
        self.btn_lock.clicked.connect(self.toggle_lock)
        inst_layout.addWidget(self.combo_symbol, stretch=1); inst_layout.addWidget(self.btn_lock)
        left_layout.addLayout(inst_layout)

        self.chk_virtual_tp = QCheckBox("ENABLE VIRTUAL TP (TURBO TRAIL)")
        self.chk_virtual_tp.setStyleSheet("color: #aaa; font-weight: bold; font-size: 8pt; margin-top: 5px; margin-bottom: 5px;")
        self.chk_virtual_tp.stateChanged.connect(self.on_virtual_tp_changed)
        left_layout.addWidget(self.chk_virtual_tp)

        lbl_log_title = QLabel("SYSTEM LOG")
        lbl_log_title.setStyleSheet("color: #666; font-size: 8pt; font-weight: bold;")
        left_layout.addWidget(lbl_log_title)
        
        self.log_display = QTextBrowser()
        self.log_display.setFixedHeight(350)
        self.log_display.setStyleSheet("background-color: #0d0d0d; color: #008888; font-family: Consolas; font-size: 9pt; border: 1px solid #222;")
        self.log_display.append("Cadet Ready.")
        left_layout.addWidget(self.log_display)
        left_layout.addStretch(1)

        # --- RIGHT COLUMN (TACTICAL HUD) ---
        right_layout = QVBoxLayout(); right_layout.setSpacing(10)

        # Header: Ticker + Master Arm
        hud_top_layout = QHBoxLayout()
        self.btn_collapse = QPushButton("◀"); self.btn_collapse.setFixedSize(30, 30); self.btn_collapse.clicked.connect(self.toggle_panel)
        self.lbl_ticker = MarqueeLabel(); self.lbl_ticker.set_custom_text("SYSTEM STANDBY", "#888888")
        
        self.btn_arm = QPushButton("SAFE"); self.btn_arm.setCheckable(True); self.btn_arm.setFixedSize(80, 35)
        self.btn_arm.setStyleSheet("background-color: #222222; color: #444444; font-weight: bold; border-radius: 4px; border: 1px solid #444444;")
        self.btn_arm.clicked.connect(self.toggle_arm)
        
        hud_top_layout.addWidget(self.btn_collapse); hud_top_layout.addWidget(self.lbl_ticker, stretch=1); hud_top_layout.addWidget(self.btn_arm)
        right_layout.addLayout(hud_top_layout)

        # VIRTUAL TP & RISK INFO
        self.order_info_frame = QFrame()
        self.order_info_frame.setFixedHeight(90)
        # FIX: Vi tog bort 'border: 1px solid #333;' och bytte till 'border: none;'
        self.order_info_frame.setStyleSheet("background-color: #111111; border: none; border-radius: 6px;")
        oi_layout = QVBoxLayout(self.order_info_frame)
        oi_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_hud_risk = QLabel("PLANNED RISK: ---")
        self.lbl_hud_risk.setStyleSheet("color: #ffaa00; font-size: 14pt; font-weight: bold; font-family: Consolas;")
        self.lbl_hud_risk.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_hud_pending = QLabel("FLAT / WAITING")
        self.lbl_hud_pending.setStyleSheet("color: #aaa; font-size: 11pt; font-family: Consolas;")
        self.lbl_hud_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.grace_bar = QProgressBar()
        self.grace_bar.setFixedHeight(4)
        self.grace_bar.setTextVisible(False)
        self.grace_bar.setRange(0, 200)
        self.grace_bar.setValue(0)
        self.grace_bar.setStyleSheet("QProgressBar { background-color: transparent; border: none; } QProgressBar::chunk { background-color: #00ffaa; }")
        self.grace_bar.hide()

        oi_layout.addWidget(self.lbl_hud_risk)
        oi_layout.addWidget(self.lbl_hud_pending)
        oi_layout.addWidget(self.grace_bar) 
        
        right_layout.addWidget(self.order_info_frame)

        # Dashboard (Anchored)
        self.tactical_frame = QFrame(); self.tactical_frame.setFixedHeight(120); self.tactical_frame.setStyleSheet("background-color: #151515; border-radius: 8px;")
        dash_layout = QHBoxLayout(self.tactical_frame); dash_layout.setContentsMargins(10, 8, 10, 8)

        p1 = QVBoxLayout(); p1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_dash_inst = QLabel("STANDBY"); self.lbl_dash_inst.setFixedWidth(130); self.lbl_dash_inst.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_dash_inst.setStyleSheet("color: #00ffff; font-weight: bold; font-family: Consolas;")
        self.lbl_size = QLabel("0"); self.lbl_size.setFixedWidth(130); self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_size.setStyleSheet("font-size: 28pt; font-weight: bold; color: #444; font-family: Consolas;")
        self.lbl_pips = QLabel("CAPACITY"); self.lbl_pips.setFixedWidth(130); self.lbl_pips.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pips.setStyleSheet("color: #555; font-size: 8pt; font-family: Consolas;")
        p1.addWidget(self.lbl_dash_inst); p1.addStretch(); p1.addWidget(self.lbl_size); p1.addWidget(self.lbl_pips); p1.addStretch()
        
        p2 = QVBoxLayout(); p2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        market_data_layout = QVBoxLayout(); market_data_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_dash_mkt = QLabel("MKT: ---"); self.lbl_dash_mkt.setStyleSheet("color: #aaa; font-family: Consolas;")
        self.lbl_dash_avg = QLabel("AVG: ---"); self.lbl_dash_avg.setStyleSheet("color: #666; font-family: Consolas;")
        market_data_layout.addWidget(self.lbl_dash_mkt); market_data_layout.addWidget(self.lbl_dash_avg)
        self.lbl_dash_state = QLabel(""); self.lbl_dash_state.setStyleSheet("font-size: 26pt;")
        p2.addLayout(market_data_layout); p2.addStretch(); p2.addWidget(self.lbl_dash_state); p2.addStretch()

        p3 = QVBoxLayout(); p3.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pnl_title = QLabel("NET POINTS"); self.lbl_pnl_title.setFixedWidth(130); self.lbl_pnl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pnl_title.setStyleSheet("color: #555; font-size: 9pt; font-family: Consolas;")
        self.lbl_pnl = QLabel("0.00"); self.lbl_pnl.setFixedWidth(130); self.lbl_pnl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pnl.setStyleSheet("font-size: 28pt; font-weight: bold; color: #444; font-family: Consolas;")
        p3.addStretch(); p3.addWidget(self.lbl_pnl_title); p3.addWidget(self.lbl_pnl); p3.addStretch()

        dash_layout.addLayout(p1, 0); dash_layout.addLayout(p2, 1); dash_layout.addLayout(p3, 0)
        right_layout.addWidget(self.tactical_frame)

        # KILL SWITCH
        self.btn_close = QPushButton("EMERGENCY CLOSE ALL"); self.btn_close.setFixedHeight(45)
        self.btn_close.setStyleSheet("background-color: #2a2a2a; color: #ff4444; font-weight: bold; border-radius: 4px; border: 1px solid #552222;")
        self.btn_close.clicked.connect(self.manager.execute_close)
        right_layout.addWidget(self.btn_close)

        self.btn_inspector = QPushButton("OPEN INSPECTOR"); self.btn_inspector.setFixedHeight(25)
        self.btn_inspector.setStyleSheet("background-color: #222; color: #888; border: 1px solid #333;")
        self.btn_inspector.clicked.connect(self.toggle_inspector)
        right_layout.addWidget(self.btn_inspector)

        self.inspector_window = TWSInspectorWindow(self)

        main_layout.addWidget(self.left_panel, stretch=1); main_layout.addLayout(right_layout, stretch=1); self.setLayout(main_layout)
        

    def reset_connection_ui(self):
        # Fas 1: Återställer UI till startläget (Vit text = Klickbart, Grå = Oklickbart)
        self.btn_connect.setText("🔗")
        self.btn_connect.setEnabled(True)
        self.btn_connect.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")
        
        self.combo_env.setEnabled(True)
        self.combo_env.setStyleSheet("QComboBox { background-color: #333333; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #555555; } QComboBox::drop-down { border: none; width: 25px; }")
        
        self.combo_symbol.setEnabled(False)
        self.combo_symbol.setStyleSheet("QComboBox { background-color: #222222; color: #666666; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #333333; } QComboBox::drop-down { border: none; width: 25px; }")
        
        self.btn_lock.setText("🔒")
        self.btn_lock.setEnabled(False)
        self.btn_lock.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")

    def on_virtual_tp_changed(self, state):
        self.manager.use_virtual_tp = (state == 2)
    
    def blink_emergency_ui(self):
        """Condition-based blinking for critical status."""
        self.emergency_blink_state = not getattr(self, 'emergency_blink_state', False)
        if self.emergency_blink_state:
            self.btn_close.setStyleSheet("background-color: #8b0000; color: white; font-weight: bold; border-radius: 4px; border: 1px solid #ff0000;")
        else:
            self.btn_close.setStyleSheet("background-color: #2a2a2a; color: #ff4444; font-weight: bold; border-radius: 4px; border: 1px solid #552222;")

    def do_connect(self):
        if getattr(self, 'alarm_active', False):
            self.alarm_active = False; self.alarm_timer.stop()
            self.reset_connection_ui()
            return
            
        if self.ib_provider.is_connected():
            # MANUELL FRÅNKOPPLING (Snyggt och utan larm)
            self._is_manual_disconnect = True
            if self.btn_lock.text() == "🔒" and self.active_instrument_name != "":
                self.toggle_lock() # Lås upp instrumentet först så Väktaren vilar
            self.ib_provider.disconnect()
            self.reset_connection_ui()
            self.update_log("SYSTEM: Connection gracefully closed.")
            return
        
        self.btn_connect.setText("⏳")
        self.btn_connect.setEnabled(False)
        self.btn_connect.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
        
        self.combo_env.setEnabled(False) 
        self.combo_env.setStyleSheet("QComboBox { background-color: #222222; color: #666666; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #333333; } QComboBox::drop-down { border: none; width: 25px; }")
        
        selected = self.combo_env.currentText()
        try: port = int(selected.split('(')[1].replace(')', ''))
        except: port = 7497
        
        self.theme_color = "#004466"
        self.ib_provider.connect({'port': port})

    def on_connection_result(self, success, account_id):
        if success:
            self.alarm_timer.stop()
            self.btn_connect.setText("⚡") 
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet(f"background-color: {self.theme_color}; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
            
            self.combo_env.setEnabled(False)
            # FIX: Vit text (#ffffff) istället för grå, behåller krispigheten i texten!
            self.combo_env.setStyleSheet(f"QComboBox:disabled {{ background-color: {self.theme_color}; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #0088aa; }} QComboBox::drop-down {{ border: none; width: 25px; }}")
            
            self.combo_symbol.setEnabled(True)
            self.combo_symbol.setStyleSheet("QComboBox { background-color: #333333; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #555555; } QComboBox::drop-down { border: none; width: 25px; }")
            
            current = self.combo_symbol.currentText()
            if current != "-- SELECT INSTRUMENT --":
                self.btn_lock.setEnabled(True)
                self.btn_lock.setText("🔓")
                self.btn_lock.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")
        else:
            self.reset_connection_ui()

    def toggle_lock(self):
        if self.btn_lock.text() == "🔓":
            name = self.combo_symbol.currentText()
            if name == "-- SELECT INSTRUMENT --": return
            
            data = self.instruments.get(name, {})
            self.active_instrument_name = name
            self.manager.trade_qty = data.get("qty", 1)
            self.manager.tp_points = data.get("tp", 10.0)
            self.manager.sl_points = data.get("sl", 5.0)
            self.manager.max_qty = data.get("max_qty", 3)
            self.manager.slippage = data.get("slippage", 2.0)
            
            if self.ib_provider.is_connected():
                self.ib_provider.set_contract(data["symbol"], data["exchange"])
            
            self.btn_lock.setText("🔒")
            self.btn_lock.setStyleSheet("background-color: #004466; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
            
            self.combo_symbol.setEnabled(False)
            # FIX: Vit text (#ffffff) här med för perfekt rendering
            self.combo_symbol.setStyleSheet("QComboBox:disabled { background-color: #004466; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #0088aa; } QComboBox::drop-down { border: none; width: 25px; }")
            
            self.btn_arm.setEnabled(True)
        else:
            self.active_instrument_name = ""
            self.manager.clear_instrument()
            
            self.btn_lock.setText("🔓")
            self.btn_lock.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")
            
            self.combo_symbol.setEnabled(True)
            self.combo_symbol.setStyleSheet("QComboBox { background-color: #333333; color: #ffffff; font-weight: bold; padding-left: 10px; border-radius: 4px; border: 1px solid #555555; } QComboBox::drop-down { border: none; width: 25px; }")
            
            self.btn_arm.setChecked(False)
            self.toggle_arm()
            self.btn_arm.setEnabled(False)

    def toggle_arm(self):
        # FIX: Critical Guard Rail
        if not self.ib_provider.is_connected() or not self.active_instrument_name:
            self.btn_arm.setChecked(False)
            self.manager.is_armed = False
            return

        is_armed = self.btn_arm.isChecked()
        self.manager.is_armed = is_armed
        
        if is_armed:
            self.btn_arm.setStyleSheet(f"background-color: {self.theme_color}; color: white; font-weight: bold;")
        else:
            self.btn_arm.setStyleSheet("background-color: #222; color: white; font-weight: bold; border: 1px solid #444;")
            
        self.manager.update_ui_state()

    def update_hud(self, data):
        # 1. Background Shift
        if data.get('is_armed', False) and self.active_instrument_name:
            self.setStyleSheet(f"background-color: {'#0a1a0a' if self.theme_color == '#2e7d32' else '#05101a'}; color: white;")
        else:
            self.setStyleSheet("background-color: #1e1e1e; color: #ffffff;")

        # 2. Condition-based UI (Anomaly Handling)
        has_anomaly = (data['pos'] != 0 or data.get('open_orders', 0) > 0)
        is_inst_locked = (self.active_instrument_name != "" and self.btn_lock.text() == "🔒")

        if has_anomaly and not self.manager.is_armed:
            if not getattr(self, '_anomaly_logged', False):
                reason = f"Open Position ({data['pos']})" if data['pos'] != 0 else f"Pending Orders ({data.get('open_orders', 0)})"
                self.update_log(f"⚠️ ANOMALY: {reason} detected while system is SAFE!")
                self._anomaly_logged = True

            if not self.emergency_timer.isActive(): self.emergency_timer.start(300)
            self.btn_arm.setText("TAKE OVER") 
            self.btn_arm.setEnabled(True) 
            self.btn_arm.setStyleSheet("background-color: #ff0000; color: white; font-weight: bold; border-radius: 4px;")
        else:
            if getattr(self, '_anomaly_logged', False):
                self.update_log("✅ ANOMALY CLEARED: System synced.")
                self._anomaly_logged = False

            if self.emergency_timer.isActive(): 
                self.emergency_timer.stop()
                self.btn_close.setStyleSheet("background-color: #2a2a2a; color: #ff4444; font-weight: bold; border-radius: 4px; border: 1px solid #552222;")
            
            self.btn_arm.setEnabled(is_inst_locked)
            
            # GUARD: Rör inte knappens färg om en varning just nu blinkar
            if not getattr(self, '_arm_warning_active', False):
                if self.manager.is_armed:
                    self.btn_arm.setText("ARMED")
                    self.btn_arm.setStyleSheet(f"background-color: {self.theme_color}; color: white; font-weight: bold; border-radius: 4px; border: 1px solid #0088aa;")
                    
                    # NY GUARD RAIL: Inaktivera disconnect och unlock när vi är ARMED!
                    self.btn_connect.setEnabled(False)
                    self.btn_connect.setStyleSheet("background-color: #222222; color: #555555; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
                    self.btn_lock.setEnabled(False)
                    self.btn_lock.setStyleSheet("background-color: #222222; color: #555555; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
                else:
                    self.btn_arm.setText("SAFE")
                    arm_text = "white" if is_inst_locked else "#444"
                    self.btn_arm.setStyleSheet(f"background-color: #222222; color: {arm_text}; font-weight: bold; border-radius: 4px; border: 1px solid #444444;")
                    
                    # Återställ knapparna när vi går tillbaka till SAFE
                    if self.ib_provider.is_connected():
                        self.btn_connect.setEnabled(True)
                        self.btn_connect.setStyleSheet(f"background-color: {self.theme_color}; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
                    
                    if is_inst_locked:
                        self.btn_lock.setEnabled(True)
                        self.btn_lock.setStyleSheet("background-color: #004466; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
                    elif self.active_instrument_name == "" and self.ib_provider.is_connected():
                        self.btn_lock.setEnabled(True)
                        self.btn_lock.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")

        # 3. Textbaserad Risk- och Pending-display med Grace Period & Cooldown
        if data['pos'] == 0:
            self.lbl_hud_risk.setText(f"PLANNED RISK: {data['sl_pts']:.2f} pts")
            self.grace_bar.hide()
            
            if data.get('pt_cooldown', False):
                self.lbl_hud_pending.setText(f"COOLDOWN: {data['pt_remaining']}s ⏳")
                # GUARD: Rör inte cooldown-lådans färg om en varning blinkar
                if not getattr(self, '_cooldown_warning_active', False):
                    self.lbl_hud_pending.setStyleSheet("color: #ffaa00; font-size: 12pt; font-weight: bold; font-family: Consolas; background-color: transparent;")
            elif data.get('pending_entry', 0.0) > 0.0:
                self.lbl_hud_pending.setText(f"PENDING ENTRY: {data['pending_entry']:.2f}  |  HARD STOP: {data['pending_sl']:.2f}")
                self.lbl_hud_pending.setStyleSheet("color: #00ff00; font-size: 11pt; font-family: Consolas; background-color: transparent;")
            else:
                self.lbl_hud_pending.setText("FLAT / WAITING")
                self.lbl_hud_pending.setStyleSheet("color: #555; font-size: 11pt; font-family: Consolas; background-color: transparent;")
        else:
            lock_icon = "🔒" if data.get('sl_locked', False) else "🔓"
            self.lbl_hud_risk.setText(f"LIVE RISK: {data['sl_pts']:.2f} pts {lock_icon}")
            
            if not data.get('sl_locked', False):
                self.grace_bar.show()
                self.grace_bar.setValue(data.get('grace_remaining', 0))
            else:
                self.grace_bar.hide()
                
            tp_text = f"VIRTUAL TP: {self.manager.virtual_tp:.2f}" if self.manager.use_virtual_tp and self.manager.virtual_tp > 0.0 else "MANUAL TP (NO TARGET)"
            self.lbl_hud_pending.setText(f"POSITION LIVE  |  {tp_text}")
            self.lbl_hud_pending.setStyleSheet("color: #00ffff; font-size: 11pt; font-family: Consolas; background-color: transparent;")

        self.chk_virtual_tp.setEnabled(not data['is_armed'])
        
        # 4. Dashboard Updates
        curr_q = abs(data['pos'])
        self.lbl_dash_inst.setText(self.active_instrument_name if self.active_instrument_name else "CADET")
        self.lbl_size.setText(str(curr_q))
        self.lbl_pnl.setText(f"{data['pl']:+.2f}")
        self.lbl_dash_mkt.setText(f"MKT: {data['price']:.2f}" if data['price'] > 0 else "MKT: ---")
        self.lbl_dash_avg.setText(f"AVG: {data['avg']:.2f}" if data['pos'] != 0 else "AVG: ---")
        
        if curr_q > 0:
            self.lbl_size.setStyleSheet(f"font-size: 28pt; font-weight: bold; color: {'#44ff44' if data['pos'] > 0 else '#ff4444'}; font-family: Consolas;")
            self.lbl_pnl.setStyleSheet(f"font-size: 28pt; font-weight: bold; color: {'#00ff00' if data['pl'] > 0 else '#ff4444' if data['pl'] < 0 else '#aaa'}; font-family: Consolas;")
            if data.get('turbo_mode'): self.lbl_dash_state.setText("🔥")
            elif data.get('trail_active'): self.lbl_dash_state.setText("🚀")
            else: self.lbl_dash_state.setText("⚡")
        else:
            self.lbl_size.setStyleSheet("font-size: 28pt; font-weight: bold; color: #444; font-family: Consolas;")
            self.lbl_pnl.setStyleSheet("font-size: 28pt; font-weight: bold; color: #444; font-family: Consolas;")
            self.lbl_dash_state.setText("")

        self.inspector_window.update_orders(
            data.get('tws_orders', []), 
            data.get('other_activity', []), 
            data.get('multi_sl_warning', False)
        )
        if data.get('multi_sl_warning'):
            self.lbl_dash_state.setText("⚠")
            self.lbl_dash_state.setStyleSheet("color: #ff4444; font-size: 26pt;")

    def on_instrument_selected(self, name):
        if name == "-- SELECT INSTRUMENT --":
            self.btn_lock.setEnabled(False)
            self.btn_lock.setText("🔒")
            self.btn_lock.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
            return
            
        if self.ib_provider.is_connected():
            self.btn_lock.setEnabled(True)
            self.btn_lock.setText("🔓")
            self.btn_lock.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")

    def toggle_panel(self):
        if self.left_panel.isVisible():
            self.left_panel.hide(); self.btn_collapse.setText("▶"); self.setFixedSize(self.collapsed_width, self.height())
        else:
            self.left_panel.show(); self.btn_collapse.setText("◀"); self.setFixedSize(self.expanded_width, self.height())

    def toggle_inspector(self):
        if self.inspector_window.isVisible():
            self.inspector_window.hide()
        else:
            self.inspector_window.show()

    def update_log(self, text):
        log_str = f"[{time.strftime('%H:%M:%S')}] {text}"
        
        # 1. Terminal Echo (Din "Svarta Låda")
        print(log_str)
        
        # 2. Uppdatera GUI (Lägger till längst ner och scrollar automatiskt)
        self.log_display.append(log_str)
        
        # Rensar gammalt om den blir extremt lång, för att spara RAM
        if self.log_display.document().blockCount() > 200:
            cursor = self.log_display.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar() # Tar bort tomraden
            
        self.lbl_ticker.set_custom_text(text, "#00ffff" if "READY" in text.upper() else "#888888")

    def reset_sl_warning(self):
        # Återställ till standard orange text
        self.lbl_hud_risk.setStyleSheet("color: #ffaa00; font-size: 14pt; font-weight: bold; font-family: Consolas; background-color: transparent;")

    def blink_arm_warning(self):
        self._arm_warning_active = True
        self.btn_arm.setText("ARM FIRST!")
        self.btn_arm.setStyleSheet("background-color: #8b0000; color: white; font-weight: bold; border: 1px solid #ff0000;")
        QTimer.singleShot(1000, self.reset_arm_warning)

    def reset_arm_warning(self):
        self._arm_warning_active = False
        self.manager.update_ui_state()

    def blink_cooldown_warning(self):
        self._cooldown_warning_active = True
        self.lbl_hud_pending.setStyleSheet("color: #ffffff; font-size: 12pt; font-weight: bold; font-family: Consolas; background-color: #8b0000; border-radius: 4px;")
        QTimer.singleShot(1000, self.reset_cooldown_warning)

    def reset_cooldown_warning(self):
        self._cooldown_warning_active = False
        self.manager.update_ui_state()


    def setup_connections(self):
        self.manager.log_signal.connect(self.update_log); self.manager.ui_update.connect(self.update_hud)
        self.manager.connection_status.connect(self.on_connection_result)
        self.manager.sl_reject_signal.connect(self.blink_sl_warning)
        self.manager.arm_reject_signal.connect(self.blink_arm_warning)
        self.manager.cooldown_reject_signal.connect(self.blink_cooldown_warning)

    def blink_sl_warning(self):
        # Visuell smäll på fingrarna!
        self.lbl_hud_risk.setStyleSheet("color: #ffffff; font-size: 14pt; font-weight: bold; font-family: Consolas; background-color: #8b0000; border-radius: 4px;")
        QTimer.singleShot(300, self.reset_sl_warning)

    
    def setup_hotkeys(self):
        # Aktiverar the thread-safe global hotkeys
        self.global_hotkeys = GlobalHotkeyManager(self)

    def load_instruments(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instruments.json")
        template = {"symbol": "MNQ", "exchange": "CME", "qty": 1, "tp": 20.0, "sl": 10.0, "max_qty": 3, "slippage": 2.0}
        if os.path.exists(path):
            try:
                with open(path, 'r') as f: self.instruments = json.load(f).get("instruments", {})
            except: self.instruments = {"MNQ": template}
        self.combo_symbol.clear(); self.combo_symbol.addItem("-- SELECT INSTRUMENT --"); self.combo_symbol.addItems(sorted(self.instruments.keys()))

    def handle_connection_lost(self):
        self.ib_provider.disconnect()
        self.manager.is_armed = False
        
        # Om det var vi själva som stängde ner det via knappen
        if getattr(self, '_is_manual_disconnect', False):
            self._is_manual_disconnect = False
            # Behöver inte göra något, reset_connection_ui har redan körts
        else:
            # Om det var en krasch eller fel
            self.btn_arm.setEnabled(False)
            self.btn_connect.setText("⚠")
            self.alarm_timer.start(500)

    def blink_connection_alarm(self):
        self.alarm_state = not getattr(self, 'alarm_state', False)
        if self.alarm_state:
            self.btn_connect.setStyleSheet("background-color: #ff0000; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #ff0000;")
        else:
            self.btn_connect.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")

    def load_settings(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    s = json.load(f)
                    idx = self.combo_env.findText(s.get("last_connection", ""))
                    if idx >= 0: self.combo_env.setCurrentIndex(idx)
                    idx = self.combo_symbol.findText(s.get("last_instrument", ""))
                    if idx >= 0: self.combo_symbol.setCurrentIndex(idx)
                    if s.get("use_virtual_tp", False):
                        self.chk_virtual_tp.setChecked(True)
            except: pass

    def save_settings(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        try:
            with open(path, 'w') as f:
                json.dump({
                    "last_connection": self.combo_env.currentText(), 
                    "last_instrument": self.combo_symbol.currentText(),
                    "use_virtual_tp": self.chk_virtual_tp.isChecked()
                }, f, indent=4)
        except: pass


    def closeEvent(self, event):
        self.save_settings(); event.accept()

    def pump_events(self):
        try: asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.01))
        except: pass

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MjolnirGUI(); window.show()
    t = QTimer(); t.timeout.connect(window.pump_events); t.start(20)
    sys.exit(app.exec())