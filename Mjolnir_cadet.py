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

class DOMWidget(QWidget):
    """The core graphics engine for the Price Ladder - Pro Jigsaw Layout."""
    
    # Nya DOM-signaler
    sig_dom_place_order = pyqtSignal(str, str, float) # action, order_type, price
    sig_dom_modify_qty = pyqtSignal(str, float)       # action, price
    sig_dom_move_order = pyqtSignal(str, float)       # order_ref, price
    sig_dom_cancel_order = pyqtSignal(str, float)     # target, price

    def __init__(self, parent=None):
        super().__init__(parent)
        self.center_price = 0.0 
        self.current_price = 0.0 
        self.bid_price, self.ask_price = 0.0, 0.0
        self.bid_size, self.ask_size = 0, 0
        self.min_tick = 0.25
        self.pixels_per_point = 80  
        
        self.my_buys = {}
        self.my_sells = {}
        self.my_stop_buys = {}
        self.my_stop_sells = {}
        
        self.pos_qty = 0
        self.avg_price = 0.0
        
        self.pending_anchor = 0.0
        self.pending_direction = 1
        
        self.pending_sl_nudge = 0.0
        self.pending_sl_side = None
        
        self.manual_levels = set()
        self.is_armed = False 
        
        self.setStyleSheet("background-color: #0d0d0d;")

    def mousePressEvent(self, event):
        if self.center_price == 0.0: return
        
        x, y = event.pos().x(), event.pos().y()
        if y <= 24: return # Ignorera header
        
        w, h = self.width(), self.height()
        
        # Dimensioner (måste matcha paintEvent)
        col_price_w = 80
        col_bids_w = 40
        col_asks_w = 40
        col_buys_w = 60  
        col_sells_w = 60 
        col_levels_w = 65 
        
        center_x = w / 2
        x_price = center_x - (col_price_w / 2)
        x_buys = x_price - col_buys_w
        x_sells = x_price + col_price_w
        x_levels = w - col_levels_w
        
        center_y = h / 2
        price_diff = (center_y - y) / self.pixels_per_point
        raw_price = self.center_price + price_diff
        clicked_price = round(round(raw_price / self.min_tick) * self.min_tick, 4)

        # 1. Klick i Level-kolumnen (Violetta streck, fungerar i SAFE mode)
        if x_levels <= x <= w:
            if event.button() == Qt.MouseButton.LeftButton:
                tolerance = 2.0
                level_to_remove = None
                for lvl in self.manual_levels:
                    if abs(lvl - clicked_price) <= (tolerance + 1e-9):
                        level_to_remove = lvl
                        break
                if level_to_remove is not None:
                    self.manual_levels.remove(level_to_remove)
                else:
                    self.manual_levels.add(clicked_price)
                self.update()
            return

        # ==========================================
        # THE TACTICAL DOM MATRIX (Kräver ARMED)
        # ==========================================
        if not self.is_armed: return

        # Identifiera tillstånd för att definiera Action vs Protection
        is_long = self.pos_qty > 0 or (self.pos_qty == 0 and self.pending_anchor > 0 and self.pending_direction == 1)
        is_short = self.pos_qty < 0 or (self.pos_qty == 0 and self.pending_anchor > 0 and self.pending_direction == -1)
        is_flat = not is_long and not is_short

        # 2. Klick i BUY-kolumnen
        if x_buys <= x < x_price:
            is_protection = is_short
            is_action = is_long or is_flat

            if event.button() == Qt.MouseButton.LeftButton:
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    if is_protection:
                        self.sig_dom_move_order.emit('TP', clicked_price)
                else:
                    if is_protection:
                        self.sig_dom_move_order.emit('SL', clicked_price)
                    elif is_action:
                        if clicked_price in self.my_buys or clicked_price in self.my_stop_buys:
                            self.sig_dom_modify_qty.emit('BUY', clicked_price) # Add Size
                        else:
                            # Momentum Stop vs Value Limit
                            order_type = 'STP' if clicked_price > self.current_price else 'LMT'
                            self.sig_dom_place_order.emit('BUY', order_type, clicked_price)
                            
            elif event.button() == Qt.MouseButton.RightButton:
                if is_protection:
                    if clicked_price in self.my_buys: # TP för en short ligger som BUY limit
                        self.sig_dom_cancel_order.emit('TP', clicked_price)
                elif is_action:
                    if clicked_price in self.my_buys or clicked_price in self.my_stop_buys:
                        self.sig_dom_cancel_order.emit('ENTRY', clicked_price)

        # 3. Klick i SELL-kolumnen
        elif x_sells <= x < x_sells + col_sells_w:
            is_protection = is_long
            is_action = is_short or is_flat

            if event.button() == Qt.MouseButton.LeftButton:
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    if is_protection:
                        self.sig_dom_move_order.emit('TP', clicked_price)
                else:
                    if is_protection:
                        self.sig_dom_move_order.emit('SL', clicked_price)
                    elif is_action:
                        if clicked_price in self.my_sells or clicked_price in self.my_stop_sells:
                            self.sig_dom_modify_qty.emit('SELL', clicked_price) # Add Size
                        else:
                            # Momentum Stop vs Value Limit
                            order_type = 'STP' if clicked_price < self.current_price else 'LMT'
                            self.sig_dom_place_order.emit('SELL', order_type, clicked_price)
                            
            elif event.button() == Qt.MouseButton.RightButton:
                if is_protection:
                    if clicked_price in self.my_sells: # TP för en long ligger som SELL limit
                        self.sig_dom_cancel_order.emit('TP', clicked_price)
                elif is_action:
                    if clicked_price in self.my_sells or clicked_price in self.my_stop_sells:
                        self.sig_dom_cancel_order.emit('ENTRY', clicked_price)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        
        painter.fillRect(0, 0, w, h, QColor("#0d0d0d"))

        if self.center_price == 0.0: return
        
        center_x = w / 2
        col_price_w = 80
        col_bids_w = 40
        col_asks_w = 40
        col_buys_w = 60  
        col_sells_w = 60 
        col_levels_w = 65 
        
        x_price = center_x - (col_price_w / 2)
        x_buys = x_price - col_buys_w
        x_bids = x_buys - col_bids_w
        x_sells = x_price + col_price_w
        x_asks = x_sells + col_sells_w
        x_levels = w - col_levels_w
        
        painter.fillRect(int(x_bids), 0, int(col_bids_w), h, QColor(0, 255, 150, 20)) 
        painter.fillRect(int(x_asks), 0, int(col_asks_w), h, QColor(255, 50, 50, 20)) 
        painter.fillRect(int(x_buys), 0, int(col_buys_w), h, QColor(0, 150, 255, 15)) 
        painter.fillRect(int(x_sells), 0, int(col_sells_w), h, QColor(255, 150, 0, 15)) 
        painter.fillRect(int(x_levels), 0, int(col_levels_w), h, QColor(179, 136, 255, 15)) 
        
        div_pen = QPen(QColor("#444444")) 
        painter.setPen(div_pen)
        painter.drawLine(int(x_bids), 0, int(x_bids), h) 
        painter.drawLine(int(x_buys), 0, int(x_buys), h) 
        painter.drawLine(int(x_price), 0, int(x_price), h) 
        painter.drawLine(int(x_sells), 0, int(x_sells), h) 
        painter.drawLine(int(x_asks), 0, int(x_asks), h) 
        painter.drawLine(int(x_asks + col_asks_w), 0, int(x_asks + col_asks_w), h) 
        painter.drawLine(int(x_levels), 0, int(x_levels), h) 

        center_y = h / 2
        points_visible_half = (h / 2) / self.pixels_per_point
        max_price = self.center_price + points_visible_half
        min_price = self.center_price - points_visible_half
        
        start_price = math.ceil(min_price / self.min_tick) * self.min_tick
        end_price = math.floor(max_price / self.min_tick) * self.min_tick
        
        if self.pixels_per_point < 18:
            painter.setFont(QFont("Consolas", 8, QFont.Weight.Bold)) 
        else:
            painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold)) 
            
        grid_pen = QPen(QColor("#1a1a1a"))
        grid_pen_strong = QPen(QColor("#2a2a2a")) 
        text_pen = QPen(QColor("#888888"))
        
        metrics = painter.fontMetrics()
        th = metrics.height() 

        if self.pos_qty != 0 and self.avg_price > 0 and self.current_price > 0:
            y_avg = int(center_y - ((self.avg_price - self.center_price) * self.pixels_per_point))
            y_curr = int(center_y - ((self.current_price - self.center_price) * self.pixels_per_point))
            
            top_y = min(y_avg, y_curr)
            zone_h = abs(y_avg - y_curr)
            
            is_profit = False
            if self.pos_qty > 0 and self.current_price >= self.avg_price: is_profit = True
            elif self.pos_qty < 0 and self.current_price <= self.avg_price: is_profit = True
            
            zone_color = QColor(0, 255, 100, 30) if is_profit else QColor(255, 50, 50, 30)
            zone_x = int(x_buys)
            zone_w = int(col_buys_w + col_price_w + col_sells_w)
            
            painter.fillRect(zone_x, top_y, zone_w, zone_h, zone_color)
            
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(zone_x, y_avg, zone_x + zone_w, y_avg)
        
        anchor_price = self.avg_price if self.pos_qty != 0 else self.pending_anchor
        direction = 1 if self.pos_qty > 0 else (-1 if self.pos_qty < 0 else self.pending_direction)
        scale_anchor = round(anchor_price / self.min_tick) * self.min_tick if anchor_price > 0 else 0.0

        p = start_price
        while p <= end_price + (self.min_tick / 2):
            price_diff = p - self.center_price
            y = int(center_y - (price_diff * self.pixels_per_point))
            row_height = self.pixels_per_point * self.min_tick
            point_height = self.pixels_per_point
            
            p_round = round(p, 4)
            is_current = abs(p - self.current_price) < (self.min_tick * 0.1)
            is_bid = abs(p - self.bid_price) < (self.min_tick * 0.1)
            is_ask = abs(p - self.ask_price) < (self.min_tick * 0.1)
            is_avg_price = (self.pos_qty != 0) and abs(p - self.avg_price) < (self.min_tick * 0.1)
            
            is_pending_sl = False
            if self.pending_sl_nudge > 0.0 and abs(p - self.pending_sl_nudge) < (self.min_tick * 0.1):
                is_pending_sl = True
            
            box_y = int(y - max(row_height, th + 4)/2)
            box_h = int(max(row_height, th + 4))
            
            if is_current:
                painter.fillRect(int(x_buys), box_y, int(col_buys_w + col_price_w + col_sells_w), box_h, QColor("#00334d"))
                
            should_draw_line = False
            current_pen = grid_pen
            if row_height >= 6: should_draw_line = True 
            elif point_height >= 8:
                if p % 1.0 == 0: should_draw_line = True; current_pen = grid_pen_strong
            else:
                if p % 5.0 == 0: should_draw_line = True; current_pen = grid_pen_strong
            
            if should_draw_line or is_current:
                painter.setPen(current_pen)
                painter.drawLine(0, y, w, y)
                
            if p_round in self.manual_levels:
                dash_pen = QPen(QColor("#b388ff")) 
                dash_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(dash_pen)
                painter.drawLine(int(x_bids), y, int(x_levels), y) 
                
                painter.fillRect(int(x_levels), box_y, int(col_levels_w), box_h, QColor(179, 136, 255, 60))
                painter.fillRect(int(x_levels), box_y, 3, box_h, QColor("#b388ff"))
                
                painter.setPen(QPen(QColor("#ffffff")))
                lw = metrics.horizontalAdvance("M")
                painter.drawText(int(x_levels + (col_levels_w - lw)/2), y + int(th/3), "M")
            
            should_draw_text = False
            if row_height >= th * 1.2: should_draw_text = True 
            elif point_height >= th * 1.5:
                if p % 1.0 == 0: should_draw_text = True 
            elif point_height * 5 >= th * 1.2:
                if p % 5.0 == 0: should_draw_text = True
            else:
                if p % 10.0 == 0: should_draw_text = True 
                
            if should_draw_text:
                if is_current:
                    painter.setPen(QPen(QColor("#00ffff"))) 
                elif p % 1.0 == 0: 
                    painter.setPen(QPen(QColor("#dddddd")))
                else: 
                    painter.setPen(text_pen)
                    
                price_str = f"{p:.2f}"
                tw = metrics.horizontalAdvance(price_str)
                painter.drawText(int(center_x - (tw / 2)), y + int(th/3), price_str)

            if scale_anchor > 0.0 and should_draw_text:
                pts = (p_round - scale_anchor) * direction
                pts_str = f"{pts:+.2f}" if pts != 0 else " 0.00"
                
                pts_color = QColor("#55cc55") if pts > 0 else QColor("#cc5555") if pts < 0 else QColor("#888888")
                painter.setPen(QPen(pts_color))
                pw = metrics.horizontalAdvance(pts_str)
                
                if direction == 1: # Long
                    painter.drawText(int(x_buys + (col_buys_w - pw)/2), y + int(th/3), pts_str)
                elif direction == -1: # Short
                    painter.drawText(int(x_sells + (col_sells_w - pw)/2), y + int(th/3), pts_str)
            
            if is_bid and self.bid_size > 0:
                painter.setPen(QPen(QColor("#00ffcc"))) 
                bw = metrics.horizontalAdvance(str(int(self.bid_size)))
                painter.drawText(int(x_bids + (col_bids_w - bw)/2), y + int(th/3), str(int(self.bid_size)))
                
            if is_ask and self.ask_size > 0:
                painter.setPen(QPen(QColor("#ff4444"))) 
                aw = metrics.horizontalAdvance(str(int(self.ask_size)))
                painter.drawText(int(x_asks + (col_asks_w - aw)/2), y + int(th/3), str(int(self.ask_size)))

            if p_round in self.my_buys:
                b_qty = str(self.my_buys[p_round])
                painter.fillRect(int(x_buys+2), box_y+2, int(col_buys_w-4), box_h-4, QColor("#0088cc")) 
                painter.setPen(QPen(QColor("#ffffff")))
                tw = metrics.horizontalAdvance(b_qty)
                painter.drawText(int(x_buys + (col_buys_w - tw)/2), y + int(th/3), b_qty)
                
            if p_round in self.my_sells:
                s_qty = str(self.my_sells[p_round])
                painter.fillRect(int(x_sells+2), box_y+2, int(col_sells_w-4), box_h-4, QColor("#cc4400")) 
                painter.setPen(QPen(QColor("#ffffff")))
                tw = metrics.horizontalAdvance(s_qty)
                painter.drawText(int(x_sells + (col_sells_w - tw)/2), y + int(th/3), s_qty)

            if p_round in self.my_stop_buys:
                qty_str = f"SL {self.my_stop_buys[p_round]}"
                painter.fillRect(int(x_buys+2), box_y+2, int(col_buys_w-4), box_h-4, QColor("#002233"))
                painter.setPen(QPen(QColor("#00ffcc"), 1))
                painter.drawRect(int(x_buys+2), box_y+2, int(col_buys_w-4), box_h-4)
                painter.setPen(QPen(QColor("#00ffcc")))
                tw = metrics.horizontalAdvance(qty_str)
                painter.drawText(int(x_buys + (col_buys_w - tw)/2), y + int(th/3), qty_str)

            if p_round in self.my_stop_sells:
                qty_str = f"SL {self.my_stop_sells[p_round]}"
                painter.fillRect(int(x_sells+2), box_y+2, int(col_sells_w-4), box_h-4, QColor("#330000"))
                painter.setPen(QPen(QColor("#ff4444"), 1))
                painter.drawRect(int(x_sells+2), box_y+2, int(col_sells_w-4), box_h-4)
                painter.setPen(QPen(QColor("#ff4444")))
                tw = metrics.horizontalAdvance(qty_str)
                painter.drawText(int(x_sells + (col_sells_w - tw)/2), y + int(th/3), qty_str)

            if is_pending_sl:
                qty_str = f"SL {max(1, abs(self.pos_qty))}"
                painter.setPen(QPen(QColor("#888888"), 1, Qt.PenStyle.DashLine))
                if self.pending_sl_side == 'BUY':
                    painter.fillRect(int(x_buys+2), box_y+2, int(col_buys_w-4), box_h-4, QColor("#1a1a1a")) 
                    painter.drawRect(int(x_buys+2), box_y+2, int(col_buys_w-4), box_h-4)
                    painter.setPen(QPen(QColor("#888888")))
                    tw = metrics.horizontalAdvance(qty_str)
                    painter.drawText(int(x_buys + (col_buys_w - tw)/2), y + int(th/3), qty_str)
                elif self.pending_sl_side == 'SELL':
                    painter.fillRect(int(x_sells+2), box_y+2, int(col_sells_w-4), box_h-4, QColor("#1a1a1a")) 
                    painter.drawRect(int(x_sells+2), box_y+2, int(col_sells_w-4), box_h-4)
                    painter.setPen(QPen(QColor("#888888")))
                    tw = metrics.horizontalAdvance(qty_str)
                    painter.drawText(int(x_sells + (col_sells_w - tw)/2), y + int(th/3), qty_str)

            if is_avg_price:
                pos_str = f"POS {abs(self.pos_qty)}"
                if self.pos_qty > 0:
                    painter.fillRect(int(x_buys+1), box_y+1, int(col_buys_w-2), box_h-2, QColor("#00ff66"))
                    painter.setPen(QPen(QColor("#000000"))) 
                    tw = metrics.horizontalAdvance(pos_str)
                    painter.drawText(int(x_buys + (col_buys_w - tw)/2), y + int(th/3), pos_str)
                else:
                    painter.fillRect(int(x_sells+1), box_y+1, int(col_sells_w-2), box_h-2, QColor("#ff3333"))
                    painter.setPen(QPen(QColor("#ffffff"))) 
                    tw = metrics.horizontalAdvance(pos_str)
                    painter.drawText(int(x_sells + (col_sells_w - tw)/2), y + int(th/3), pos_str)
                
            p += self.min_tick

        header_h = 24
        header_bg = QColor("#004466") if self.is_armed else QColor(20, 20, 20, 255)
        painter.fillRect(0, 0, w, header_h, header_bg) 
        
        painter.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        painter.setPen(QPen(QColor("#cccccc") if self.is_armed else QColor("#888888")))
        
        def draw_header(text, x, width):
            tw = metrics.horizontalAdvance(text)
            painter.drawText(int(x + (width - tw)/2), 16, text)

        draw_header("BID", x_bids, col_bids_w)
        draw_header("BUY", x_buys, col_buys_w)
        draw_header("PRICE", x_price, col_price_w)
        draw_header("SEL", x_sells, col_sells_w)
        draw_header("ASK", x_asks, col_asks_w)
        draw_header("LVL", x_levels, col_levels_w)
        
        painter.setPen(QPen(QColor("#444444") if not self.is_armed else QColor("#0088aa")))
        painter.drawLine(0, header_h, w, header_h)

class MjolnirDOMWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("MJÖLNIR DOM")
        self.resize(450, 800) 
        self.setStyleSheet("background-color: #151515;")
        self.manager = parent.manager if parent else None
        self.main_gui = parent 
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(0) 
        
        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setText("<b><span style='color: #888888;'>DOM</span></b> &nbsp;|&nbsp; <b><span style='color: #555555;'>STANDBY</span></b>")
        self.header.setStyleSheet("background-color: #151515; font-family: Consolas; font-size: 10pt; padding: 4px; border-top-left-radius: 4px; border-top-right-radius: 4px;")
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.header)
        
        self.dom_widget = DOMWidget()
        layout.addWidget(self.dom_widget, stretch=1)

        # Koppla signalerna till Väktarens hjärna
        if self.manager:
            self.dom_widget.sig_dom_place_order.connect(self.manager.handle_dom_place_order)
            self.dom_widget.sig_dom_modify_qty.connect(self.manager.handle_dom_modify_qty)
            self.dom_widget.sig_dom_move_order.connect(self.manager.move_order_to_price)
            self.dom_widget.sig_dom_cancel_order.connect(self.manager.cancel_dom_order)
        
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 5, 0, 0)
        
        self.btn_arm = QPushButton("SAFE")
        self.btn_arm.setCheckable(True)
        self.btn_arm.setFixedSize(65, 25)
        self.btn_arm.setStyleSheet("background-color: #222222; color: #aaaaaa; font-family: Consolas; font-weight: bold; font-size: 8pt; border-radius: 4px; border: 1px solid #444444;")
        self.btn_arm.clicked.connect(self.toggle_main_arm)
        
        lbl_scale = QLabel("ZOOM:")
        lbl_scale.setStyleSheet("color: #cccccc; font-family: Consolas; font-size: 8pt; font-weight: bold;")
        
        self.slider_scale = QSlider(Qt.Orientation.Horizontal)
        self.slider_scale.setRange(8, 80) 
        self.slider_scale.setValue(80)
        self.slider_scale.setFixedWidth(60)
        self.slider_scale.valueChanged.connect(self.on_scale_changed)
        
        self.lbl_points = QLabel("(-- pts)")
        self.lbl_points.setFixedWidth(80) 
        self.lbl_points.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_points.setStyleSheet("color: #cccccc; font-family: Consolas; font-size: 8pt; font-weight: bold; margin-left: 5px;")
        
        self.btn_clear_levels = QPushButton("CLR LVLS")
        self.btn_clear_levels.setFixedSize(65, 25) 
        self.btn_clear_levels.setStyleSheet("background-color: #222; color: #cccccc; border: 1px solid #444; font-family: Consolas; font-size: 8pt; border-radius: 4px;")
        self.btn_clear_levels.clicked.connect(self.clear_manual_levels)
        
        footer_layout.addWidget(self.btn_arm)
        footer_layout.addStretch()         
        footer_layout.addWidget(lbl_scale)
        footer_layout.addWidget(self.slider_scale)
        footer_layout.addWidget(self.lbl_points)
        footer_layout.addStretch()         
        footer_layout.addWidget(self.btn_clear_levels) 
        
        layout.addLayout(footer_layout)

    def toggle_main_arm(self):
        if self.main_gui and hasattr(self.main_gui, 'btn_arm'):
            self.main_gui.btn_arm.click()
            
    def clear_manual_levels(self):
        self.dom_widget.manual_levels.clear()
        self.dom_widget.update()

    def update_points_label(self):
        if self.dom_widget.pixels_per_point > 0:
            pts = self.dom_widget.height() / self.dom_widget.pixels_per_point
            self.lbl_points.setText(f"({pts:.1f} pts)")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_points_label()

    def on_scale_changed(self, val):
        self.dom_widget.pixels_per_point = val
        self.update_points_label()
        self.dom_widget.update()
        
    def recenter(self):
        self.update_dom({'price': self.manager.current_price if self.manager else 0.0}, self.dom_widget.min_tick)
        if self.dom_widget.current_price > 0:
            self.dom_widget.center_price = self.dom_widget.current_price
            self.dom_widget.update()
        
    def update_dom(self, data, min_tick):
        self.dom_widget.min_tick = min_tick
        
        current = data.get('price', 0.0)
        self.dom_widget.current_price = current
        self.dom_widget.bid_price = data.get('bid', 0.0)
        self.dom_widget.ask_price = data.get('ask', 0.0)
        self.dom_widget.bid_size = data.get('bid_size', 0)
        self.dom_widget.ask_size = data.get('ask_size', 0)
        
        is_armed = data.get('is_armed', False)
        self.dom_widget.is_armed = is_armed
        
        self.btn_arm.setChecked(is_armed)
        if is_armed:
            self.btn_arm.setText("ARMED")
            self.btn_arm.setStyleSheet("background-color: #004466; color: white; font-family: Consolas; font-weight: bold; font-size: 8pt; border-radius: 4px; border: 1px solid #0088aa;")
            self.header.setStyleSheet("background-color: #004466; font-family: Consolas; font-size: 10pt; padding: 4px; border-top-left-radius: 4px; border-top-right-radius: 4px;")
        else:
            self.btn_arm.setText("SAFE")
            self.btn_arm.setStyleSheet("background-color: #222222; color: #aaaaaa; font-family: Consolas; font-weight: bold; font-size: 8pt; border-radius: 4px; border: 1px solid #444444;")
            self.header.setStyleSheet("background-color: #151515; font-family: Consolas; font-size: 10pt; padding: 4px; border-top-left-radius: 4px; border-top-right-radius: 4px;")
        
        self.dom_widget.pos_qty = data.get('pos', 0)
        raw_avg = data.get('avg', 0.0)
        
        if self.dom_widget.pos_qty > 0:
            display_avg = math.ceil(raw_avg / min_tick) * min_tick
        elif self.dom_widget.pos_qty < 0:
            display_avg = math.floor(raw_avg / min_tick) * min_tick
        else:
            display_avg = 0.0
            
        self.dom_widget.avg_price = round(display_avg, 4)
        
        self.dom_widget.pending_anchor = data.get('pending_entry', 0.0)
        self.dom_widget.pending_direction = data.get('display_direction', 1)
        
        self.dom_widget.my_buys = data.get('my_buys', {})
        self.dom_widget.my_sells = data.get('my_sells', {})
        self.dom_widget.my_stop_buys = data.get('my_stop_buys', {})   
        self.dom_widget.my_stop_sells = data.get('my_stop_sells', {}) 
        
        self.dom_widget.pending_sl_nudge = data.get('pending_sl_nudge', 0.0)
        self.dom_widget.pending_sl_side = data.get('pending_sl_side', None)
        
        if getattr(self, '_auto_center', True) and current > 0:
            self.dom_widget.center_price = current
            self._auto_center = False 
            
        self.dom_widget.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0: return

        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            step = 5 if delta > 0 else -5
            new_val = self.slider_scale.value() + step
            self.slider_scale.setValue(new_val) 
        else:
            self._auto_center = False 
            points_to_move = (delta / 120.0) * (self.dom_widget.min_tick * 4) 
            
            if self.dom_widget.center_price > 0:
                self.dom_widget.center_price += points_to_move
                self.dom_widget.update()

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
    def place_bracket(self, action: str, qty: int, lmt_price: float, tp_price: float, sl_price: float, entry_type: str = 'LMT'): pass
    @abstractmethod
    def place_single_order(self, action: str, qty: int, price: float, order_ref: str, order_type: str = 'LMT'): pass
    @abstractmethod
    def cancel_all(self): pass
    @abstractmethod
    def modify_order(self, order_ref: str, new_price: float, new_qty: Optional[int] = None): pass
    @abstractmethod
    def cancel_order_by_id(self, order_id: int): pass
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
            error_str = str(e).lower()
            friendly_err = "Unknown Error"
            if not error_str or "timeout" in error_str:
                friendly_err = f"Timeout on port {port}. API enabled in TWS?"
            elif "refused" in error_str or "1225" in error_str:
                friendly_err = f"Connection Refused on {port}. Is TWS/Gateway running?"
            else:
                friendly_err = str(e)
                
            self.signals.connection_confirmed.emit(False, friendly_err)
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

    def place_bracket(self, action: str, qty: int, lmt_price: float, tp_price: float, sl_price: float, entry_type: str = 'LMT'):
        if not self.contract or not self.is_connected(): return
        bracket = self.ib.bracketOrder(action, qty, lmt_price, lmt_price, sl_price)
        entry, sl = bracket[0], bracket[2]
        
        if entry_type == 'STP':
            entry.orderType = 'STP'
            entry.auxPrice = lmt_price
            entry.lmtPrice = 0.0
            
        entry.orderRef, sl.orderRef = "ENTRY", "SL"
        entry.tif = sl.tif = 'GTC'
        entry.outsideRth = sl.outsideRth = True
        entry.usePriceMgmtAlgo = True
        entry.transmit = False
        sl.transmit = True 
        self.ib.placeOrder(self.contract, entry)
        self.ib.placeOrder(self.contract, sl)
        self.signals.status_msg.emit(f"SENT: {action} {qty} ({entry_type} Bracket Active)")

    def place_single_order(self, action: str, qty: int, price: float, order_ref: str, order_type: str = 'LMT'):
        if not self.contract or not self.is_connected(): return
        
        if order_type == 'STP':
            order = StopOrder(action, qty, price, outsideRth=True, tif='GTC')
        else:
            order = LimitOrder(action, qty, price, outsideRth=True, tif='GTC')
            order.usePriceMgmtAlgo = True
            
        order.orderRef = order_ref
        order.transmit = True  
        self.ib.placeOrder(self.contract, order)

    def cancel_all(self):
        if not self.contract or not self.is_connected(): return
        count = 0
        for t in self.ib.openTrades():
            if t.contract.conId == self.contract.conId:
                if t.orderStatus.status not in ['Cancelled', 'Filled', 'Inactive', 'ApiCancelled', 'PendingCancel']:
                    self.ib.cancelOrder(t.order)
                    count += 1
                    
        if count > 0: self.signals.status_msg.emit(f"CLEANUP: {count} active/pending orders erased.")

    def get_active_order_count(self) -> int:
        if not self.contract or not self.is_connected(): return 0
        return len([t for t in self.ib.openTrades() if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates])

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
            for t in self.ib.openTrades():
                if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                    if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                        target_action = "SELL" if t.order.action == "BUY" else "BUY"
                        break
            
        for t in self.ib.openTrades():
            if t.contract.conId == self.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                
                if t.order.orderRef == order_ref:
                    return getattr(t.order, 'auxPrice', getattr(t.order, 'lmtPrice', 0.0))
                
                if target_action and t.order.action == target_action:
                    if order_ref == 'SL' and t.order.orderType in ['STP', 'STP LMT', 'TRAIL']:
                        return t.order.auxPrice
                    elif order_ref == 'TP' and t.order.orderType == 'LMT' and t.order.parentId != 0:
                        return t.order.lmtPrice
                        
        return 0.0
    
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
                        if t.order.orderType == 'STP LMT':
                            price_diff = new_price - t.order.auxPrice
                            t.order.lmtPrice = round(t.order.lmtPrice + price_diff, 4)
                        t.order.auxPrice = new_price
                        
                    elif order_ref == 'TP':
                        t.order.lmtPrice = new_price
                        
                    if new_qty is not None:
                        t.order.totalQuantity = new_qty
                        
                    self.ib.placeOrder(t.contract, t.order)
                    return 

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

# REPLACE
class SentinelManager(QObject):
    log_signal = pyqtSignal(str)
    ui_update = pyqtSignal(dict)
    connection_status = pyqtSignal(bool, str)
    connection_lost_signal = pyqtSignal()
    flash_signal = pyqtSignal(str)
    sl_reject_signal = pyqtSignal()
    arm_reject_signal = pyqtSignal()      
    cooldown_reject_signal = pyqtSignal() 
    max_qty_reject_signal = pyqtSignal()

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
        self.grace_time_remaining = 200 
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
            
            self.grace_timer.stop()
            self.sl_locked = False
            
        self.avg_price = self.pure_avg_price if self.pure_avg_price > 0 else a
            
        if self.pos_qty == 0 and q != 0:
            self.entry_time = time.time()
            self.trail_active = False 
            self.turbo_mode = False
            self.peak_price = self.current_price
            self.current_trail_distance = self.trail_points
            
            self._start_grace_period()
            QTimer.singleShot(200, lambda: self._perform_auto_snap(q))
            
        self.pos_qty = q
        self.update_ui_state()

    def _perform_auto_snap(self, q):
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

        if self.pos_qty > 0: 
            target_action = "SELL"
        elif self.pos_qty < 0: 
            target_action = "BUY"
        else:
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

        has_multiple_sl = False
        expected_sl = 0.0 
        current_active_sl = 0.0 

        if pending_anchor > 0.0:
            expected_sl = round(round((pending_anchor - (self.sl_points * pending_direction)) / self.min_tick) * self.min_tick, 4)

        if self.is_armed and len(active_stops) > 0:
            master_sl = active_stops[0]
            current_active_sl = master_sl['price']

            if self.pos_qty == 0 and pending_anchor > 0.0:
                if getattr(self, '_last_pending_anchor', 0.0) != pending_anchor:
                    self._last_pending_anchor = pending_anchor
                    if abs(master_sl['price'] - expected_sl) > (self.min_tick * 0.1):
                        for p in self.providers:
                            if p.is_connected():
                                p.modify_order('SL', expected_sl)
                                self.log_signal.emit(f"MAGNETIC: Snapped pending SL to {self.sl_points:.2f} pts.")

            elif self.pos_qty != 0:
                self._last_pending_anchor = 0.0 
                if master_sl['qty'] != abs(self.pos_qty):
                    for p in self.providers:
                        if p.is_connected():
                            p.modify_order('SL', master_sl['price'], new_qty=abs(self.pos_qty))
                            self.log_signal.emit(f"SYNC: Adjusted Master SL to {abs(self.pos_qty)} contracts.")

            if len(active_stops) > 1:
                has_multiple_sl = True
                for extra_sl in active_stops[1:]:
                    for p in self.providers:
                        if p.is_connected() and hasattr(p, 'cancel_order_by_id'):
                            p.cancel_order_by_id(extra_sl['id'])
                            self.log_signal.emit(f"MERGE: Cancelled overlapping SL order.")
        else:
            self._last_pending_anchor = 0.0
            current_active_sl = expected_sl

        secured_pts = 0.0
        if self.pos_qty != 0:
            direction = 1 if self.pos_qty > 0 else -1
            if current_active_sl > 0.0:
                secured_pts = (current_active_sl - self.avg_price) * direction
            else:
                secured_pts = -self.sl_points
        else:
            secured_pts = -self.sl_points

        bid_p, ask_p, bid_s, ask_s = 0.0, 0.0, 0, 0
        for p in self.providers:
            if p.is_connected() and hasattr(p, 'mkt_data') and p.mkt_data:
                bid_p = p.mkt_data.bid if p.mkt_data.bid and not math.isnan(p.mkt_data.bid) else 0.0
                ask_p = p.mkt_data.ask if p.mkt_data.ask and not math.isnan(p.mkt_data.ask) else 0.0
                bid_s = p.mkt_data.bidSize if p.mkt_data.bidSize and not math.isnan(p.mkt_data.bidSize) else 0
                ask_s = p.mkt_data.askSize if p.mkt_data.askSize and not math.isnan(p.mkt_data.askSize) else 0

        working_buys = {}
        working_sells = {}
        working_stops_buy = {}  
        working_stops_sell = {} 
        
        for p in self.providers:
            if p.is_connected() and p.contract:
                for trade in p.ib.openTrades():
                    if trade.contract.conId == p.contract.conId:
                        if trade.orderStatus.status not in ['Filled', 'Cancelled', 'Inactive', 'ApiCancelled']:
                            side = trade.order.action
                            qty = trade.order.totalQuantity - trade.orderStatus.filled
                            
                            is_stop = trade.order.orderType in ['STP', 'STP LMT', 'TRAIL']
                            if is_stop:
                                raw_p = getattr(trade.order, 'auxPrice', 0.0)
                            else:
                                raw_p = getattr(trade.order, 'lmtPrice', 0.0)
                                
                            price_lvl = raw_p if (raw_p and 0 < raw_p < 1e100) else 0.0
                            
                            if price_lvl > 0 and qty > 0:
                                p_snap = round(round(price_lvl / self.min_tick) * self.min_tick, 4)
                                if is_stop:
                                    if side == 'BUY':
                                        working_stops_buy[p_snap] = working_stops_buy.get(p_snap, 0) + int(qty)
                                    else:
                                        working_stops_sell[p_snap] = working_stops_sell.get(p_snap, 0) + int(qty)
                                else:
                                    if side == 'BUY':
                                        working_buys[p_snap] = working_buys.get(p_snap, 0) + int(qty)
                                    else:
                                        working_sells[p_snap] = working_sells.get(p_snap, 0) + int(qty)

        pending_sl_nudge = self.pending_nudges.get('SL', 0.0)
        pending_sl_side = None
        if pending_sl_nudge > 0.0:
            if self.pos_qty > 0: pending_sl_side = 'SELL'
            elif self.pos_qty < 0: pending_sl_side = 'BUY'
            elif target_action: pending_sl_side = target_action

            if current_active_sl > 0.0:
                old_sl_snap = round(round(current_active_sl / self.min_tick) * self.min_tick, 4)
                if pending_sl_side == 'SELL' and old_sl_snap in working_stops_sell:
                    del working_stops_sell[old_sl_snap]
                elif pending_sl_side == 'BUY' and old_sl_snap in working_stops_buy:
                    del working_stops_buy[old_sl_snap]

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
            'sl_locked': getattr(self, 'sl_locked', False),
            'grace_remaining': getattr(self, 'grace_time_remaining', 0),
            'secured_pts': secured_pts,
            'bid': bid_p, 'ask': ask_p, 'bid_size': bid_s, 'ask_size': ask_s,
            'my_buys': working_buys, 'my_sells': working_sells,
            'my_stop_buys': working_stops_buy, 'my_stop_sells': working_stops_sell,
            'pending_sl_nudge': pending_sl_nudge, 
            'pending_sl_side': pending_sl_side   
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

        if self.use_virtual_tp and self.virtual_tp > 0.0 and not self.turbo_mode:
            if (direction == 1 and p >= self.virtual_tp) or (direction == -1 and p <= self.virtual_tp):
                self.log_signal.emit(f"🎯 VIRTUAL TP HIT ({self.virtual_tp:.2f}): Activating Turbo Trail!")
                self.trail_active = True
                self.turbo_mode = True
                self.current_trail_distance = self.tight_trail_points
                self.peak_price = p 
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

    def process_trailing_stop(self):
        if self.pos_qty == 0: return
        direction = 1 if self.pos_qty > 0 else -1
        target_sl = round(round((self.peak_price - (self.current_trail_distance * direction)) / self.min_tick) * self.min_tick, 4)
        
        for p in self.providers:
            if p.is_connected():
                current_sl = self.pending_nudges.get('SL', p.get_order_price('SL'))
                if current_sl and ((direction == 1 and target_sl > current_sl) or (direction == -1 and target_sl < current_sl)):
                    self.pending_nudges['SL'] = target_sl
                    self.nudge_timer.start(400)

    def handle_error(self, c, m):
        self.log_signal.emit(f"API ERROR [{c}]: {m}")

    def is_connected(self) -> bool:
        return any(p.is_connected() for p in self.providers)

    def clear_instrument(self):
        self.current_price = 0.0
        for p in self.providers:
            if hasattr(p, 'clear_contract'): p.clear_contract()
        self.update_ui_state()

    def _get_pending_direction(self) -> int:
        for p in self.providers:
            if p.is_connected() and p.contract:
                for t in p.ib.openTrades():
                    if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                        if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                            return 1 if t.order.action == "BUY" else -1
        return 0

    def _is_over_max_capacity(self, side: int, add_qty: int) -> bool:
        current_pending = 0
        for p in self.providers:
            if p.is_connected() and p.contract:
                for t in p.ib.openTrades():
                    if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                        if t.order.parentId == 0 and t.order.action == ("BUY" if side == 1 else "SELL"):
                            current_pending += (int(t.order.totalQuantity) - int(t.orderStatus.filled))
        return (abs(self.pos_qty) + current_pending + add_qty) > self.max_qty

    def _check_pre_fill_scale_violation(self, action: str, new_price: float) -> bool:
        """Ser till att nya ordrar alltid läggs SÄMRE än the Master Anchor för att säkra SL"""
        if self.pos_qty != 0: return False 
        
        anchor_price = 0.0
        anchor_type = 'LMT'
        for p in self.providers:
            if p.is_connected() and p.contract:
                for t in p.ib.openTrades():
                    if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                        # Identifierar The Master Anchor via dess orderRef "ENTRY"
                        if t.order.parentId == 0 and t.order.action == action and t.order.orderRef == "ENTRY":
                            anchor_price = getattr(t.order, 'auxPrice', 0.0) if t.order.orderType in ['STP'] else getattr(t.order, 'lmtPrice', 0.0)
                            anchor_type = t.order.orderType
                            break
                            
        if anchor_price > 0.0:
            if anchor_type in ['STP', 'STP LMT']:
                if action == "BUY" and new_price <= anchor_price: return True
                if action == "SELL" and new_price >= anchor_price: return True
            else:
                if action == "BUY" and new_price >= anchor_price: return True
                if action == "SELL" and new_price <= anchor_price: return True
                
        return False

    def execute_trade(self, action: str):
        if not self.is_armed:
            self.arm_reject_signal.emit()
            self.log_signal.emit("REJECTED: System is SAFE. Arm first.")
            return
            
        if getattr(self, 'post_trade_cooldown_active', False):
            self.cooldown_reject_signal.emit()
            self.log_signal.emit("REJECTED: Post-trade cooldown active. ⏳")
            return

        if self.cooldown: return
        self.cooldown = True
        QTimer.singleShot(400, lambda: setattr(self, 'cooldown', False))
        
        qty = self.trade_qty
        side = 1 if action == "BUY" else -1
        
        slip_ticks = max(2, math.ceil(self.slippage / self.min_tick))
        lmt = round(self.current_price + (slip_ticks * self.min_tick * side), 4)

        if (self.pos_qty > 0 and side == -1) or (self.pos_qty < 0 and side == 1):
            if abs(self.pos_qty) <= qty:
                self.execute_close()
                return
                
        if self._is_over_max_capacity(side, qty):
            self.max_qty_reject_signal.emit() 
            self.log_signal.emit(f"REJECTED: Max Qty ({self.max_qty}) reached.")
            return
            
        if self._check_pre_fill_scale_violation(action, lmt):
            self.cooldown_reject_signal.emit()
            self.log_signal.emit("GUARD RAIL: Scale must be placed BEHIND Anchor! ⛔")
            return
            
        pending_dir = self._get_pending_direction()
        is_scaling = False
        
        if self.pos_qty != 0:
            is_scaling = True 
        else:
            if pending_dir != 0:
                if side == pending_dir:
                    is_scaling = True
                else:
                    self.log_signal.emit("REJECTED: Cancel opposite pending orders first.")
                    return

        if is_scaling:
            for p in self.providers:
                if p.is_connected(): p.place_single_order(action, qty, lmt, "SCALE")
        else:
            sl_price = round(round((self.current_price - (self.sl_points * side)) / self.min_tick) * self.min_tick, 4)
            for p in self.providers:
                if p.is_connected(): p.place_bracket(action, qty, lmt, 0.0, sl_price)

    def _sniper_entry(self, action: str):
        if not self.is_armed:
            self.arm_reject_signal.emit()
            self.log_signal.emit("REJECTED: System is SAFE. Arm first.")
            return

        qty = self.trade_qty
        side = 1 if action == "BUY" else -1
        
        exact_p = 0.0
        for p in self.providers:
            if p.is_connected() and hasattr(p, 'mkt_data') and p.mkt_data:
                if action == "BUY":
                    exact_p = p.mkt_data.bid if p.mkt_data.bid and not math.isnan(p.mkt_data.bid) else 0.0
                else:
                    exact_p = p.mkt_data.ask if p.mkt_data.ask and not math.isnan(p.mkt_data.ask) else 0.0
        
        if exact_p <= 0.0:
            exact_p = self.current_price

        if (self.pos_qty > 0 and side == -1) or (self.pos_qty < 0 and side == 1):
            self.execute_close()
            return

        if self._is_over_max_capacity(side, qty):
            self.max_qty_reject_signal.emit()
            self.log_signal.emit(f"REJECTED: Max Qty ({self.max_qty}) reached.")
            return
            
        lmt = round(exact_p, 4)
        
        if self._check_pre_fill_scale_violation(action, lmt):
            self.cooldown_reject_signal.emit()
            self.log_signal.emit("GUARD RAIL: Scale must be placed BEHIND Anchor! ⛔")
            return
            
        pending_dir = self._get_pending_direction()
        is_scaling = False
        
        if self.pos_qty != 0:
            is_scaling = True
        else:
            if pending_dir != 0:
                if side == pending_dir:
                    is_scaling = True
                else:
                    self.log_signal.emit("REJECTED: Cancel opposite pending orders first.")
                    return

        if is_scaling:
            for p in self.providers:
                if p.is_connected(): p.place_single_order(action, qty, lmt, "SCALE")
            self.log_signal.emit(f"SNIPER: Joined {action} at {lmt:.2f} (SCALE)")
        else:
            sl_price = round(round((lmt - (self.sl_points * side)) / self.min_tick) * self.min_tick, 4)
            for p in self.providers:
                if p.is_connected(): p.place_bracket(action, qty, lmt, 0.0, sl_price)
            self.log_signal.emit(f"SNIPER: Joined {action} at {lmt:.2f} with Guard SL at {sl_price:.2f}")

    def execute_join_bid(self): self._sniper_entry("BUY")
    def execute_join_ask(self): self._sniper_entry("SELL")

    def execute_cancel_working(self):
        count = 0
        for p in self.providers:
            if p.is_connected() and hasattr(p, 'ib'):
                for t in p.ib.openTrades():
                    if p.contract and t.contract.conId == p.contract.conId:
                        if t.orderStatus.status not in ['Cancelled', 'Filled', 'Inactive', 'ApiCancelled', 'PendingCancel']:
                            if t.order.parentId == 0 and t.order.orderType == 'LMT':
                                p.ib.cancelOrder(t.order)
                                count += 1
        
        if count > 0:
            self.log_signal.emit(f"CANCEL: Removed {count} pending entry order(s).")
        else:
            self.log_signal.emit("CANCEL: No pending entry orders found.")

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
            self.peak_price = self.current_price 
            self.log_signal.emit(f"TURBO ACTIVE: {self.tight_trail_points} pts.")
            self.process_trailing_stop()
        self.update_ui_state()

    def nudge_order(self, order_type: str, price_ticks: int):
        direction = 1 if self.pos_qty > 0 else -1
        if self.pos_qty == 0:
            for p in self.providers:
                if p.is_connected() and p.contract:
                    for t in p.ib.openTrades():
                        if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                            if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                                direction = 1 if t.order.action == "BUY" else -1
                                break

        if order_type == 'SL' and self.pos_qty != 0:
            if price_ticks < 0: 
                if self.sl_locked:
                    self.sl_reject_signal.emit()
                    self.log_signal.emit("GUARD RAIL: SL Retreat BLOCKED. 🔒")
                    return
            elif price_ticks > 0: 
                if not self.sl_locked:
                    self.sl_locked = True
                    self.grace_timer.stop()
                    self.grace_time_remaining = 0
                    self.log_signal.emit("CADET: Risk reduced. SL Direction Locked early. 🔒")

        current_price = 0.0
        is_live_nudge = False

        if self.is_armed and self.pos_qty != 0:
            for p in self.providers:
                if p.is_connected():
                    current_price = self.pending_nudges.get(order_type, p.get_order_price(order_type))
                    if current_price > 0.0: 
                        is_live_nudge = True
                        break

        if current_price == 0.0:
            anchor = getattr(self, '_last_pending_anchor', 0.0)
            if anchor == 0.0: return 

            if order_type == 'SL':
                current_price = anchor - (self.sl_points * direction)
            elif order_type == 'TP':
                current_price = anchor + (self.tp_points * direction)

        if current_price > 0.0:
            exact_price = round(current_price + (price_ticks * self.min_tick * direction), 4)

            # Pre-fill limit Guard Rail för Nudge
            if order_type == 'SL' and self.pos_qty == 0:
                anchor = getattr(self, '_last_pending_anchor', 0.0)
                if anchor > 0:
                    if (direction == 1 and exact_price >= anchor) or (direction == -1 and exact_price <= anchor):
                        self.sl_reject_signal.emit()
                        self.log_signal.emit("GUARD RAIL: SL cannot cross Pending Entry! ⛔")
                        return

            if not is_live_nudge:
                if order_type == 'SL':
                    self.sl_points = max(self.min_tick, self.sl_points - (price_ticks * self.min_tick))
                elif order_type == 'TP':
                    self.tp_points = max(self.min_tick, self.tp_points + (price_ticks * self.min_tick))
            else:
                if self.trail_active and order_type == 'SL':
                    new_dist = (self.peak_price - exact_price) * direction
                    if new_dist > 0:
                        self.current_trail_distance = new_dist
                        self.log_signal.emit(f"⚙️ TRAIL SYNC: Tighter distance set ({new_dist:.2f} pts)")

            self.pending_nudges[order_type] = exact_price
            self.nudge_timer.start(400)
            self._pending_log_type = order_type
            
        self.update_ui_state()

    def commit_nudges(self):
        for ref, price in self.pending_nudges.items():
            for p in self.providers:
                if p.is_connected(): p.modify_order(ref, price)
            self.log_signal.emit(f"API: Transmitted {ref} order update ({price:.2f})")
            
        self.pending_nudges.clear()

        if hasattr(self, '_pending_log_type') and self._pending_log_type:
            ref = self._pending_log_type
            self.log_signal.emit(f"CADET: {ref} limit dynamically adjusted.")
            self._pending_log_type = None

    def execute_close(self):
        for p in self.providers:
            if p.is_connected(): 
                p.cancel_all()
                if self.pos_qty != 0:
                    action = "SELL" if self.pos_qty > 0 else "BUY"
                    side = 1 if action == "BUY" else -1
                    
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

    # =========================================================================
    # THE NEW DOM-CLICK EVENT HANDLERS (SEPARATION OF CONCERNS)
    # =========================================================================

    def handle_dom_place_order(self, action: str, order_type: str, price: float):
        if getattr(self, 'post_trade_cooldown_active', False):
            self.cooldown_reject_signal.emit()
            self.log_signal.emit("REJECTED: Post-trade cooldown active. ⏳")
            return

        if self.cooldown: return
        self.cooldown = True
        QTimer.singleShot(400, lambda: setattr(self, 'cooldown', False))

        qty = self.trade_qty
        side = 1 if action == "BUY" else -1

        if self._is_over_max_capacity(side, qty):
            self.max_qty_reject_signal.emit()
            self.log_signal.emit(f"REJECTED: Max Qty ({self.max_qty}) reached.")
            return

        pending_dir = self._get_pending_direction()
        is_scaling = False

        if self.pos_qty != 0:
            if (self.pos_qty > 0 and side == 1) or (self.pos_qty < 0 and side == -1):
                is_scaling = True
            else:
                return # Block opposite click
        else:
            if pending_dir != 0:
                if side == pending_dir:
                    is_scaling = True
                else:
                    self.log_signal.emit("REJECTED: Cancel opposite pending orders first.")
                    return

        # VÄKTARENS NYA SPÄRR
        if self._check_pre_fill_scale_violation(action, price):
            self.cooldown_reject_signal.emit() # Får Order Status att blinka rött
            self.log_signal.emit("GUARD RAIL: Scale must be placed BEHIND Anchor! ⛔")
            return

        if is_scaling:
            for p in self.providers:
                if p.is_connected(): p.place_single_order(action, qty, price, "SCALE", order_type=order_type)
            self.log_signal.emit(f"DOM: Sent {action} {order_type} @ {price:.2f} (SCALE)")
        else:
            sl_price = round(round((price - (self.sl_points * side)) / self.min_tick) * self.min_tick, 4)
            for p in self.providers:
                if p.is_connected(): p.place_bracket(action, qty, price, 0.0, sl_price, entry_type=order_type)
            self.log_signal.emit(f"DOM: Sent {action} {order_type} @ {price:.2f} with Guard SL at {sl_price:.2f}")

    def handle_dom_modify_qty(self, action: str, price: float):
        qty_increase = self.trade_qty
        side = 1 if action == "BUY" else -1
        
        if self._is_over_max_capacity(side, qty_increase):
            self.max_qty_reject_signal.emit()
            self.log_signal.emit(f"REJECTED: Max Qty ({self.max_qty}) reached.")
            return
            
        for p in self.providers:
            if p.is_connected():
                for t in p.ib.openTrades():
                    if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                        if t.order.parentId == 0 and t.order.action == action:
                            current_p = getattr(t.order, 'auxPrice', 0.0) if t.order.orderType in ['STP'] else getattr(t.order, 'lmtPrice', 0.0)
                            if abs(current_p - price) < (self.min_tick * 0.1):
                                new_qty = t.order.totalQuantity + qty_increase
                                t.order.totalQuantity = new_qty
                                p.ib.placeOrder(t.contract, t.order)
                                self.log_signal.emit(f"DOM: Scaled {action} order to {new_qty} contracts.")
                                return

    def move_order_to_price(self, order_type: str, new_price: float):
        direction = 1 if self.pos_qty > 0 else -1
        if self.pos_qty == 0:
            for p in self.providers:
                if p.is_connected() and p.contract:
                    for t in p.ib.openTrades():
                        if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                            if t.order.parentId == 0 and t.order.orderType in ['LMT', 'STP']:
                                direction = 1 if t.order.action == "BUY" else -1
                                break

        # 1. RETREAT LOCK GUARD RAIL
        if order_type == 'SL' and self.pos_qty != 0:
            current_sl = 0.0
            for p in self.providers:
                if p.is_connected():
                    current_sl = self.pending_nudges.get('SL', p.get_order_price('SL'))
                    if current_sl > 0: break

            if current_sl > 0:
                is_retreat = (direction == 1 and new_price < current_sl) or (direction == -1 and new_price > current_sl)
                if is_retreat and self.sl_locked:
                    self.sl_reject_signal.emit()
                    self.log_signal.emit("GUARD RAIL: SL Retreat BLOCKED. 🔒")
                    return
                elif not is_retreat and not self.sl_locked:
                    self.sl_locked = True
                    self.grace_timer.stop()
                    self.grace_time_remaining = 0
                    self.log_signal.emit("CADET: Risk reduced. SL Direction Locked early. 🔒")

        # 2. THE HARD LIMIT GUARD RAIL (MAX 20 POINTS RISK)
        if order_type == 'SL':
            anchor = self.avg_price if self.pos_qty != 0 else getattr(self, '_last_pending_anchor', 0.0)
            if anchor > 0:
                max_pts = 20.0
                worst_allowed = anchor - (max_pts * direction)
                if (direction == 1 and new_price < worst_allowed) or (direction == -1 and new_price > worst_allowed):
                    self.sl_reject_signal.emit()
                    self.log_signal.emit(f"GUARD RAIL: Hard Risk Limit! Max {max_pts} pts. ⛔")
                    return

        # 3. PRE-FILL CROSS GUARD RAIL (Can't move SL past entry)
        if order_type == 'SL' and self.pos_qty == 0:
            anchor = getattr(self, '_last_pending_anchor', 0.0)
            if anchor > 0:
                if (direction == 1 and new_price >= anchor) or (direction == -1 and new_price <= anchor):
                    self.sl_reject_signal.emit()
                    self.log_signal.emit("GUARD RAIL: SL cannot cross Pending Entry! ⛔")
                    return

        # 4. COMMIT NUDGE
        self.pending_nudges[order_type] = new_price
        self.nudge_timer.start(200) # Fast UI update
        self._pending_log_type = order_type

        anchor = self.avg_price if self.pos_qty != 0 else getattr(self, '_last_pending_anchor', 0.0)
        if anchor > 0:
            if order_type == 'SL':
                self.sl_points = max(self.min_tick, (anchor - new_price) * direction)
            elif order_type == 'TP':
                self.tp_points = max(self.min_tick, (new_price - anchor) * direction)
                
        self.update_ui_state()

    def cancel_dom_order(self, target: str, price: float):
        for p in self.providers:
            if p.is_connected():
                for t in p.ib.openTrades():
                    if t.contract.conId == p.contract.conId and t.orderStatus.status not in OrderStatus.DoneStates:
                        current_p = getattr(t.order, 'auxPrice', 0.0) if t.order.orderType in ['STP', 'STP LMT', 'TRAIL'] else getattr(t.order, 'lmtPrice', 0.0)
                        if abs(current_p - price) < (self.min_tick * 0.1):
                            if target == 'TP' and t.order.orderRef == 'TP':
                                p.ib.cancelOrder(t.order)
                                self.log_signal.emit(f"DOM: Cancelled TP @ {price:.2f}")
                                return
                            elif target == 'ENTRY' and t.order.parentId == 0:
                                # Om the Master Anchor klickas bort, döda alla scale-ordrar för att inte lämna dig naken!
                                if self.pos_qty == 0 and t.order.orderRef == "ENTRY":
                                    count = 0
                                    for p_sub in self.providers:
                                        for t_sub in p_sub.ib.openTrades():
                                            if t_sub.contract.conId == p.contract.conId and t_sub.order.parentId == 0 and t_sub.orderStatus.status not in OrderStatus.DoneStates:
                                                if t_sub.order.action == t.order.action:
                                                    p_sub.ib.cancelOrder(t_sub.order)
                                                    count += 1
                                    self.log_signal.emit(f"DOM: Cancelled {count} linked entries to protect scales.")
                                else:
                                    p.ib.cancelOrder(t.order)
                                    self.log_signal.emit(f"DOM: Cancelled Entry @ {price:.2f}")
                                return                     

class GlobalHotkeyManager(QObject):
    sig_arm = pyqtSignal()
    sig_trade = pyqtSignal(str)
    sig_close = pyqtSignal()
    sig_trail = pyqtSignal()
    sig_be = pyqtSignal()
    sig_nudge = pyqtSignal(str, int)
    
    # SNIPER & DOM SIGNALS
    sig_join_bid = pyqtSignal()
    sig_join_ask = pyqtSignal()
    sig_cancel_working = pyqtSignal()
    sig_recenter_dom = pyqtSignal() 

    def __init__(self, gui):
        super().__init__()
        self.gui = gui
        self.manager = gui.manager
        
        # 1. Connect signals to UI/Manager
        self.sig_arm.connect(self.gui.btn_arm.click)
        self.sig_trade.connect(self.manager.execute_trade)
        self.sig_close.connect(self.manager.execute_close)
        self.sig_trail.connect(self.manager.escalate_trail)
        self.sig_be.connect(self.manager.execute_be_move)
        self.sig_nudge.connect(self.manager.nudge_order)
        
        # 2. Setup the global keyboard hooks
        keyboard.add_hotkey('ctrl+shift+a', self.sig_arm.emit)
        keyboard.add_hotkey('ctrl+shift+b', lambda: self.sig_trade.emit("BUY"))
        keyboard.add_hotkey('ctrl+shift+s', lambda: self.sig_trade.emit("SELL"))
        keyboard.add_hotkey('ctrl+shift+c', self.sig_close.emit)
        keyboard.add_hotkey('ctrl+shift+t', self.sig_trail.emit)
        keyboard.add_hotkey('ctrl+shift+e', self.sig_be.emit)
        
        # RISK MANAGEMENT HOTKEYS 
        keyboard.add_hotkey('ctrl+shift+I', lambda: self.sig_nudge.emit('SL', -1))
        keyboard.add_hotkey('ctrl+shift+K', lambda: self.sig_nudge.emit('SL', 1))
        
        # SNIPER HOTKEYS
        keyboard.add_hotkey('ctrl+shift+F5', self.sig_join_bid.emit)
        keyboard.add_hotkey('ctrl+shift+F6', self.sig_join_ask.emit)
        keyboard.add_hotkey('ctrl+shift+F7', self.sig_cancel_working.emit)
        
        # DOM HOTKEYS
        keyboard.add_hotkey('ctrl+shift+F12', self.sig_recenter_dom.emit)


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
        
        self.ammo_timer = QTimer()
        self.ammo_timer.timeout.connect(self.blink_ammo_ui)
        self.ammo_blink_state = False
        self.ammo_blink_count = 0
        self.is_maxed = False

        self.setup_hotkeys()
        self.setup_connections()
        

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

        tools_layout = QHBoxLayout()
        tools_layout.setSpacing(5)
        
        self.btn_inspector = QPushButton("🔍 INSP")
        self.btn_inspector.setFixedSize(65, 25)
        self.btn_inspector.setStyleSheet("background-color: #222; color: #888; border: 1px solid #333; font-weight: bold; border-radius: 4px;")
        self.btn_inspector.clicked.connect(self.toggle_inspector)

        self.btn_dom = QPushButton("📊 DOM")
        self.btn_dom.setFixedSize(65, 25)
        self.btn_dom.setStyleSheet("background-color: #222; color: #888; border: 1px solid #333; font-weight: bold; border-radius: 4px;")
        self.btn_dom.clicked.connect(self.toggle_dom)

        self.dom_height_preset = 800 
        self.btn_dom_height = QPushButton(f"↕ {self.dom_height_preset}px")
        self.btn_dom_height.setFixedSize(85, 25)
        self.btn_dom_height.setStyleSheet("background-color: #222; color: #888; border: 1px solid #333; font-family: Consolas; font-size: 8pt; border-radius: 4px;")
        self.btn_dom_height.clicked.connect(self.cycle_dom_height)
        
        tools_layout.addWidget(self.btn_inspector)
        tools_layout.addWidget(self.btn_dom)
        tools_layout.addWidget(self.btn_dom_height)
        left_layout.addLayout(tools_layout)
        

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
        
        hud_top_layout.addWidget(self.btn_collapse)
        hud_top_layout.addWidget(self.lbl_ticker, stretch=1)
        hud_top_layout.addWidget(self.btn_arm)
        right_layout.addLayout(hud_top_layout)


        # ==========================================
        # THE VERTICAL TACTICAL HUD
        # ==========================================
        self.tactical_frame = QFrame()
        self.tactical_frame.setStyleSheet("background-color: #151515; border-radius: 8px;")
        dash_layout = QVBoxLayout(self.tactical_frame)
        dash_layout.setContentsMargins(15, 15, 15, 15)
        dash_layout.setSpacing(10)

        # --- BLOCK 1: SIZE ---
        size_layout = QVBoxLayout()
        size_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_dash_inst = QLabel("SIZE")
        self.lbl_dash_inst.setStyleSheet("color: #999; font-size: 10pt; font-family: Consolas; font-weight: bold;")
        self.lbl_dash_inst.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_size = QLabel("0")
        self.lbl_size.setStyleSheet("font-size: 32pt; font-weight: bold; color: #777; font-family: Consolas;")
        self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pips = QLabel("CAPACITY")
        self.lbl_pips.setStyleSheet("color: #999; font-size: 10pt; font-family: Consolas;")
        self.lbl_pips.setAlignment(Qt.AlignmentFlag.AlignCenter)
        size_layout.addWidget(self.lbl_dash_inst)
        size_layout.addWidget(self.lbl_size)
        size_layout.addWidget(self.lbl_pips)
        dash_layout.addLayout(size_layout)

        line1 = QFrame(); line1.setFrameShape(QFrame.Shape.HLine); line1.setStyleSheet("background-color: #333;")
        dash_layout.addWidget(line1)

        # --- BLOCK 2: PNL ---
        pnl_layout = QVBoxLayout()
        pnl_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pnl_title = QLabel("OPEN PNL")
        self.lbl_pnl_title.setStyleSheet("color: #999; font-size: 10pt; font-family: Consolas; font-weight: bold;")
        self.lbl_pnl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pnl = QLabel("0.00")
        self.lbl_pnl.setStyleSheet("font-size: 32pt; font-weight: bold; color: #777; font-family: Consolas;")
        self.lbl_pnl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        mkt_avg_layout = QHBoxLayout()
        self.lbl_dash_mkt = QLabel("MKT: ---"); self.lbl_dash_mkt.setMinimumWidth(130)
        self.lbl_dash_mkt.setStyleSheet("color: #888; font-family: Consolas; font-size: 10pt;")
        self.lbl_dash_mkt.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        
        self.lbl_dash_state = QLabel("🛡️"); self.lbl_dash_state.setMinimumWidth(40)
        self.lbl_dash_state.setStyleSheet("font-size: 14pt;")
        self.lbl_dash_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_dash_avg = QLabel("AVG: ---"); self.lbl_dash_avg.setMinimumWidth(130)
        self.lbl_dash_avg.setStyleSheet("color: #888; font-family: Consolas; font-size: 10pt;")
        self.lbl_dash_avg.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        mkt_avg_layout.addWidget(self.lbl_dash_mkt)
        mkt_avg_layout.addWidget(self.lbl_dash_state)
        mkt_avg_layout.addWidget(self.lbl_dash_avg)
        
        pnl_layout.addWidget(self.lbl_pnl_title)
        pnl_layout.addWidget(self.lbl_pnl)
        pnl_layout.addLayout(mkt_avg_layout)
        dash_layout.addLayout(pnl_layout)

        line2 = QFrame(); line2.setFrameShape(QFrame.Shape.HLine); line2.setStyleSheet("background-color: #333;")
        dash_layout.addWidget(line2)

        # --- BLOCK 3: RISK ---
        risk_layout = QVBoxLayout()
        risk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_trade_status_title = QLabel("PLANNED RISK")
        self.lbl_trade_status_title.setStyleSheet("color: #999; font-size: 10pt; font-family: Consolas; font-weight: bold;")
        self.lbl_trade_status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_trade_status = QLabel("0.00")
        self.lbl_trade_status.setStyleSheet("font-size: 32pt; font-weight: bold; color: #ffaa00; font-family: Consolas;")
        self.lbl_trade_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        risk_details_layout = QHBoxLayout()
        self.lbl_left_price = QLabel("ENTRY: ---"); self.lbl_left_price.setMinimumWidth(130)
        self.lbl_left_price.setStyleSheet("color: #888; font-family: Consolas; font-size: 10pt;")
        self.lbl_left_price.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        
        self.lbl_status_icon = QLabel("🔓"); self.lbl_status_icon.setMinimumWidth(40)
        self.lbl_status_icon.setStyleSheet("font-size: 14pt;")
        self.lbl_status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_right_price = QLabel("STOP: ---"); self.lbl_right_price.setMinimumWidth(130)
        self.lbl_right_price.setStyleSheet("color: #888; font-family: Consolas; font-size: 10pt;")
        self.lbl_right_price.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        risk_details_layout.addWidget(self.lbl_left_price)
        risk_details_layout.addWidget(self.lbl_status_icon)
        risk_details_layout.addWidget(self.lbl_right_price)
        
        risk_layout.addWidget(self.lbl_trade_status_title)
        risk_layout.addWidget(self.lbl_trade_status)
        risk_layout.addLayout(risk_details_layout)
        dash_layout.addLayout(risk_layout)

        right_layout.addWidget(self.tactical_frame)

        # --- TRAIL CONFIG BAR ---
        self.lbl_trail_config = QLabel("⚙️ TRAIL CONFIG: ---")
        self.lbl_trail_config.setStyleSheet("color: #666; font-family: Consolas; font-size: 10pt;")
        self.lbl_trail_config.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_trail_config)

        # VIRTUAL TP & GRACE INFO (Behåller för grace baren)
        self.order_info_frame = QFrame()
        self.order_info_frame.setFixedHeight(50)
        self.order_info_frame.setStyleSheet("background-color: #111111; border: none; border-radius: 6px;")
        oi_layout = QVBoxLayout(self.order_info_frame)
        oi_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_hud_pending = QLabel("FLAT / WAITING")
        self.lbl_hud_pending.setStyleSheet("color: #999; font-size: 11pt; font-family: Consolas;") 
        self.lbl_hud_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.grace_bar = QProgressBar()
        self.grace_bar.setFixedHeight(4)
        self.grace_bar.setTextVisible(False)
        self.grace_bar.setRange(0, 200)
        self.grace_bar.setValue(0)
        self.grace_bar.setStyleSheet("QProgressBar { background-color: transparent; border: none; } QProgressBar::chunk { background-color: #00ffaa; }")
        self.grace_bar.hide()

        oi_layout.addWidget(self.lbl_hud_pending)
        oi_layout.addWidget(self.grace_bar) 
        
        right_layout.addWidget(self.order_info_frame)
        right_layout.addStretch(1) # Trycker ner Kill Switch till botten

        # KILL SWITCH
        self.btn_close = QPushButton("EMERGENCY CLOSE ALL"); self.btn_close.setFixedHeight(45)
        self.btn_close.setStyleSheet("background-color: #2a2a2a; color: #ff4444; font-weight: bold; border-radius: 4px; border: 1px solid #552222;")
        self.btn_close.clicked.connect(self.manager.execute_close)
        right_layout.addWidget(self.btn_close)

        # (Längst ner i init_ui)
        self.inspector_window = TWSInspectorWindow(self)
        self.dom_window = MjolnirDOMWindow(self) 


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

    def blink_ammo_ui(self):
        """Pulserar Ammo-mätaren 3 gånger när max kapacitet nås, sen lyser den fast."""
        self.ammo_blink_count += 1
        
        # Stoppa efter 6 växlingar (3 hela blinkningar)
        if self.ammo_blink_count > 6:
            self.ammo_timer.stop()
            self.lbl_pips.setStyleSheet("color: #00ffff; font-size: 12pt; font-family: Consolas;")
            return

        self.ammo_blink_state = not self.ammo_blink_state
        if self.ammo_blink_state:
            # Lyser upp
            self.lbl_pips.setStyleSheet("color: #00ffff; font-size: 12pt; font-family: Consolas;")
        else:
            # Tonar ner
            self.lbl_pips.setStyleSheet("color: #225555; font-size: 12pt; font-family: Consolas;")

    def trigger_ammo_blink(self):
        """Triggas manuellt om vi försöker handla över maxgränsen."""
        self.ammo_blink_count = 0
        if not self.ammo_timer.isActive():
            self.ammo_timer.start(300)

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
        
        # NYTT: Logga att vi börjar, och FORCERA uppritning innan tråden fryser!
        self.update_log(f"SYSTEM: Attempting connection on port {port}...")
        QApplication.processEvents() 
        
        self.ib_provider.connect({'port': port})

    def on_connection_result(self, success, account_id):
        if success:
            self.alarm_timer.stop()
            self.btn_connect.setText("⚡") 
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet(f"background-color: {self.theme_color}; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
            
            self.combo_env.setEnabled(False)
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
            # NYTT: Om det misslyckas, skriv ut varför
            self.update_log(f"❌ CONNECTION FAILED: {account_id}")
            
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
            if self.active_instrument_name:
                if not hasattr(self, 'dom_scales'): self.dom_scales = {}
                self.dom_scales[self.active_instrument_name] = self.dom_window.slider_scale.value()

        self.manager.update_ui_state()

    def update_hud(self, data):
        # 1. Background Shift 
        if data.get('is_armed', False) and self.active_instrument_name:
            self.setStyleSheet("background-color: #082540; color: white;") 
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
                    
                    self.btn_connect.setEnabled(False)
                    self.btn_connect.setStyleSheet("background-color: #222222; color: #555555; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
                    self.btn_lock.setEnabled(False)
                    self.btn_lock.setStyleSheet("background-color: #222222; color: #555555; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
                else:
                    self.btn_arm.setText("SAFE")
                    arm_text = "white" if is_inst_locked else "#444"
                    self.btn_arm.setStyleSheet(f"background-color: #222222; color: {arm_text}; font-weight: bold; border-radius: 4px; border: 1px solid #444444;")
                    
                    if self.ib_provider.is_connected():
                        self.btn_connect.setEnabled(True)
                        self.btn_connect.setStyleSheet(f"background-color: {self.theme_color}; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
                    
                    if is_inst_locked:
                        self.btn_lock.setEnabled(True)
                        self.btn_lock.setStyleSheet("background-color: #004466; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #0088aa;")
                    elif self.active_instrument_name == "" and self.ib_provider.is_connected():
                        self.btn_lock.setEnabled(True)
                        self.btn_lock.setStyleSheet("background-color: #333333; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #555555;")

        # 3. VERTICAL HUD & GRACE LOGIC
        direction = data.get('display_direction', 1)
        curr_q = abs(data['pos'])
        
        if curr_q == 0:
            self.lbl_trade_status_title.setText("PLANNED RISK")
            self.lbl_trade_status.setText(f"{data['sl_pts']:.2f}")
            
            if not getattr(self, '_sl_warning_active', False):
                self.lbl_trade_status.setStyleSheet("color: #ffaa00; font-size: 32pt; font-weight: bold; font-family: Consolas;")
                
            pending_entry = data.get('pending_entry', 0.0)
            pending_sl = data.get('pending_sl', 0.0)
            
            self.lbl_left_price.setText(f"ENTRY: {pending_entry:.2f}" if pending_entry > 0 else "ENTRY: ---")
            self.lbl_right_price.setText(f"STOP: {pending_sl:.2f}" if pending_entry > 0 else "STOP: ---")
            self.lbl_status_icon.setText("🔓")
            self.grace_bar.hide()
            
            if data.get('pt_cooldown', False):
                self.lbl_hud_pending.setText(f"COOLDOWN: {data['pt_remaining']}s ⏳")
                if not getattr(self, '_cooldown_warning_active', False):
                    self.lbl_hud_pending.setStyleSheet("color: #ffaa00; font-size: 12pt; font-weight: bold; font-family: Consolas; background-color: transparent;")
            elif pending_entry > 0.0:
                self.lbl_hud_pending.setText("LIMIT ORDER ACTIVE")
                self.lbl_hud_pending.setStyleSheet("color: #00ff00; font-size: 11pt; font-family: Consolas; background-color: transparent;")
            else:
                self.lbl_hud_pending.setText("FLAT / WAITING")
                self.lbl_hud_pending.setStyleSheet("color: #999; font-size: 11pt; font-family: Consolas; background-color: transparent;") 
        else:
            secured = data.get('secured_pts', 0.0)
            if not getattr(self, '_sl_warning_active', False):
                if secured > 0:
                    self.lbl_trade_status_title.setText("SECURED PROFIT")
                    self.lbl_trade_status.setText(f"+{secured:.2f}")
                    self.lbl_trade_status.setStyleSheet("color: #00ff00; font-size: 32pt; font-weight: bold; font-family: Consolas;")
                else:
                    self.lbl_trade_status_title.setText("LIVE RISK")
                    self.lbl_trade_status.setText(f"{abs(secured):.2f}")
                    self.lbl_trade_status.setStyleSheet("color: #ffaa00; font-size: 32pt; font-weight: bold; font-family: Consolas;")

            self.lbl_status_icon.setText("🔒" if data.get('sl_locked', False) else "🔓")
            
            # MATEMATIK: Räkna ut fysiskt SL-pris baserat på säkrade poäng
            current_sl = data['avg'] + (secured * direction)
            self.lbl_right_price.setText(f"STOP: {current_sl:.2f}")
            
            if data.get('trail_active'):
                self.lbl_left_price.setText(f"PEAK: {self.manager.peak_price:.2f}")
            elif self.manager.use_virtual_tp and self.manager.virtual_tp > 0:
                self.lbl_left_price.setText(f"TARGET: {self.manager.virtual_tp:.2f}")
            else:
                self.lbl_left_price.setText("TARGET: ---")
            
            if not data.get('sl_locked', False):
                self.grace_bar.show()
                self.grace_bar.setValue(data.get('grace_remaining', 0))
            else:
                self.grace_bar.hide()
                
            self.lbl_hud_pending.setText("POSITION LIVE")
            self.lbl_hud_pending.setStyleSheet("color: #00ffff; font-size: 11pt; font-family: Consolas; background-color: transparent;")

        self.chk_virtual_tp.setEnabled(not data['is_armed'])
        
        # 4. AMMO & DASHBOARD 
        max_q = self.manager.max_qty
        filled_boxes = min(curr_q, max_q)
        empty_boxes = max(0, max_q - curr_q)
        ammo_str = ("■ " * filled_boxes + "□ " * empty_boxes).strip()
        
        if not self.active_instrument_name:
            self.lbl_dash_inst.setText("SIZE")
            self.lbl_pips.setText("CAPACITY")
            self.lbl_pips.setStyleSheet("color: #999; font-size: 10pt; font-family: Consolas;") 
            if self.ammo_timer.isActive(): self.ammo_timer.stop()
            self.is_maxed = False
        else:
            self.lbl_dash_inst.setText(f"SIZE ({self.active_instrument_name})")
            self.lbl_pips.setText(ammo_str)
            if curr_q >= max_q:
                if not self.is_maxed:
                    self.is_maxed = True
                    self.ammo_blink_count = 0
                    if not self.ammo_timer.isActive(): self.ammo_timer.start(300)
                elif not self.ammo_timer.isActive():
                    self.lbl_pips.setStyleSheet("color: #00ffff; font-size: 12pt; font-family: Consolas;")
            elif curr_q > 0:
                self.is_maxed = False
                if self.ammo_timer.isActive(): self.ammo_timer.stop()
                self.lbl_pips.setStyleSheet("color: #00ffff; font-size: 12pt; font-family: Consolas;")
            else:
                self.is_maxed = False
                if self.ammo_timer.isActive(): self.ammo_timer.stop()
                self.lbl_pips.setStyleSheet("color: #777; font-size: 12pt; font-family: Consolas;") 

        self.lbl_size.setText(str(curr_q))
        self.lbl_pnl.setText(f"{data['pl']:+.2f}")
        self.lbl_dash_mkt.setText(f"MKT: {data['price']:.2f}" if data['price'] > 0 else "MKT: ---")
        self.lbl_dash_avg.setText(f"AVG: {data['avg']:.2f}" if data['pos'] != 0 else "AVG: ---")
        
        # Färg, Font och Hjärt-ikonen (Heartbeat)
        if curr_q > 0:
            self.lbl_size.setStyleSheet(f"font-size: 32pt; font-weight: bold; color: {'#44ff44' if data['pos'] > 0 else '#ff4444'}; font-family: Consolas;")
            self.lbl_pnl.setStyleSheet(f"font-size: 32pt; font-weight: bold; color: {'#00ff00' if data['pl'] > 0 else '#ff4444' if data['pl'] < 0 else '#aaa'}; font-family: Consolas;")
            
            if data.get('turbo_mode'): self.lbl_dash_state.setText("🔥")
            elif data.get('trail_active'): self.lbl_dash_state.setText("🚀")
            else: self.lbl_dash_state.setText("⚡")
        else:
            self.lbl_size.setStyleSheet("font-size: 32pt; font-weight: bold; color: #777; font-family: Consolas;") 
            self.lbl_pnl.setStyleSheet("font-size: 32pt; font-weight: bold; color: #777; font-family: Consolas;") 
            self.lbl_dash_state.setText("🛡️")

        # Överskrid hjärt-ikonen om det finns en kritisk varning
        if data.get('multi_sl_warning'):
            self.lbl_dash_state.setText("⚠")
            self.lbl_dash_state.setStyleSheet("color: #ff4444; font-size: 16pt;")
            
        # Uppdatera Trail Config Bar längst ner
        if data.get('trail_active'):
            dist = self.manager.current_trail_distance
            self.lbl_trail_config.setText(f"🚀 TRAILING ACTIVE (Distance: {dist:.1f} pts)")
            self.lbl_trail_config.setStyleSheet("color: #00ffff; font-family: Consolas; font-size: 10pt; font-weight: bold;")
        else:
            t_pts = self.manager.trail_points
            tb_pts = self.manager.tight_trail_points
            self.lbl_trail_config.setText(f"⚙️ TRAIL CONFIG: {t_pts:.1f} pts  (Turbo: {tb_pts:.1f} pts)")
            self.lbl_trail_config.setStyleSheet("color: #666; font-family: Consolas; font-size: 10pt;")

        self.inspector_window.update_orders(
            data.get('tws_orders', []), 
            data.get('other_activity', []), 
            data.get('multi_sl_warning', False)
        )
        if self.dom_window.isVisible():
            self.dom_window.update_dom(data, self.manager.min_tick)
            if self.active_instrument_name:
                self.dom_window.header.setText(f"MICRO-DOM ({self.active_instrument_name})")

    def on_instrument_selected(self, name):
        if name == "-- SELECT INSTRUMENT --":
            self.btn_lock.setEnabled(False)
            self.btn_lock.setText("🔒")
            self.btn_lock.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")
            return

        # Ladda sparad DOM-skala för detta instrument
        if hasattr(self, 'dom_scales'):
            saved_scale = self.dom_scales.get(name, 80)
            self.dom_window.slider_scale.setValue(saved_scale)
          
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

    def toggle_dom(self):
        if self.dom_window.isVisible():
            self.dom_window.hide()
        else:
            self.dom_window.show()

    def cycle_dom_height(self):
        heights = [800, 1080, 1440, 1800]
        current = getattr(self, 'dom_height_preset', 800)
        
        if current in heights:
            idx = (heights.index(current) + 1) % len(heights)
        else:
            idx = 0
            
        self.dom_height_preset = heights[idx]
        self.btn_dom_height.setText(f"↕ {self.dom_height_preset}px")
        
        if hasattr(self, 'dom_window'):
            current_width = self.dom_window.width()
            self.dom_window.resize(current_width, self.dom_height_preset)


    def update_log(self, text):
        log_str = f"[{time.strftime('%H:%M:%S')}] {text}"
        print(log_str)
        self.log_display.append(log_str)
        
        if self.log_display.document().blockCount() > 200:
            cursor = self.log_display.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar() 
            
        self.lbl_ticker.set_custom_text(text, "#00ffff" if "READY" in text.upper() else "#888888")

    def reset_sl_warning(self):
        self._sl_warning_active = False
        self.manager.update_ui_state()

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
        self.manager.log_signal.connect(self.update_log)
        self.manager.ui_update.connect(self.update_hud)
        self.manager.connection_status.connect(self.on_connection_result)
        self.manager.connection_lost_signal.connect(self.handle_connection_lost)
        self.manager.sl_reject_signal.connect(self.blink_sl_warning)
        self.manager.arm_reject_signal.connect(self.blink_arm_warning)
        self.manager.cooldown_reject_signal.connect(self.blink_cooldown_warning)
        self.manager.max_qty_reject_signal.connect(self.trigger_ammo_blink)
        self.global_hotkeys.sig_join_bid.connect(self.manager.execute_join_bid)
        self.global_hotkeys.sig_join_ask.connect(self.manager.execute_join_ask)
        self.global_hotkeys.sig_cancel_working.connect(self.manager.execute_cancel_working)
        self.global_hotkeys.sig_recenter_dom.connect(self.dom_window.recenter)

    def blink_sl_warning(self):
        self._sl_warning_active = True
        self.lbl_trade_status.setStyleSheet("color: #ffffff; font-size: 32pt; font-weight: bold; font-family: Consolas; background-color: #8b0000; border-radius: 4px;")
        QTimer.singleShot(300, self.reset_sl_warning)

    def setup_hotkeys(self):
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
        
        if getattr(self, '_is_manual_disconnect', False):
            self._is_manual_disconnect = False
        else:
            self.update_log("🚨 CRITICAL: CONNECTION TO TWS LOST! SYSTEM DISARMED.")
            self.btn_arm.setChecked(False) 
            self.btn_arm.setEnabled(False)
            self.btn_connect.setText("⚠")
            self.alarm_timer.start(500)
            
        self.manager.update_ui_state()

    def blink_connection_alarm(self):
        self.alarm_state = not getattr(self, 'alarm_state', False)
        if self.alarm_state:
            self.btn_connect.setStyleSheet("background-color: #ff0000; color: #ffffff; font-size: 14pt; border-radius: 4px; border: 1px solid #ff0000;")
        else:
            self.btn_connect.setStyleSheet("background-color: #222222; color: #666666; font-size: 14pt; border-radius: 4px; border: 1px solid #333333;")

    def load_settings(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.dom_scales = {} 
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
                    self.dom_scales = s.get("dom_scales", {}) 
                    
            
                    self.dom_height_preset = s.get("dom_height", 800)
                    if hasattr(self, 'btn_dom_height'):
                        self.btn_dom_height.setText(f"↕ {self.dom_height_preset}px")
                    if hasattr(self, 'dom_window'):
                        self.dom_window.resize(450, self.dom_height_preset)
                        
            except: pass

    def save_settings(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        try:
            with open(path, 'w') as f:
                json.dump({
                    "last_connection": self.combo_env.currentText(), 
                    "last_instrument": self.combo_symbol.currentText(),
                    "use_virtual_tp": self.chk_virtual_tp.isChecked(),
                    "dom_scales": getattr(self, 'dom_scales', {}),
                    "dom_height": getattr(self, 'dom_height_preset', 800) 
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