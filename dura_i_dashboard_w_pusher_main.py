import csv
from datetime import datetime
import json
import os
import subprocess
import sys
import time
from collections import deque

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg
from pymodbus.client import ModbusTcpClient

# ==========================================
# PATH & FILE CONFIGURATION
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "dura_i_full_telemetry.csv")
JSON_FILE = os.path.join(SCRIPT_DIR, "telemetry.json")

IP = "192.168.68.56"
PORT = 502
SLAVE = 1
WINDOW_SECONDS = 3600 * 12  # 12-Hour Scrolling Window for Local GUI
RETENTION_PERIOD = 86400  # 24-Hour Rolling Buffer for Web Dashboard

# Theme Palette
COLOR_PV1 = "#00E676"  # Green (SE PV String 1)
COLOR_PV2 = "#FF9100"  # Orange (NW PV String 2)
COLOR_LOAD = "#FF5252"  # Red (House Load Demand)
COLOR_BATT = "#FFD700"  # Gold (Battery Power / SOC)
COLOR_GRID = "#448AFF"  # Blue (Grid Voltage / Current)


class TimeAxisItem(pg.AxisItem):
    """Renders Epoch timestamps as HH:MM:SS on graph axes."""

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            if v > 0:
                strings.append(datetime.fromtimestamp(v).strftime("%H:%M:%S"))
            else:
                strings.append("")
        return strings


class MetricCard(QFrame):
    """Custom UI Card to highlight live telemetry metrics."""

    def __init__(self, title, accent_color, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: #181825;
                border-radius: 8px;
                border-left: 4px solid {accent_color};
                padding: 8px;
            }}
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        font_title = QFont("Sans-Serif", 9, QFont.Weight.Bold)
        font_val = QFont("Sans-Serif", 15, QFont.Weight.Bold)
        font_sub = QFont("Sans-Serif", 8)

        self.lbl_title = QLabel(title)
        self.lbl_title.setFont(font_title)
        self.lbl_title.setStyleSheet("color: #BAC2DE; border: none;")

        self.lbl_val = QLabel("--")
        self.lbl_val.setFont(font_val)
        self.lbl_val.setStyleSheet("color: #CDD6F4; border: none;")

        self.lbl_sub = QLabel("Reading...")
        self.lbl_sub.setFont(font_sub)
        self.lbl_sub.setStyleSheet("color: #A6ADC8; border: none;")

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_val)
        layout.addWidget(self.lbl_sub)
        self.setLayout(layout)

    def update_data(self, val_str, sub_str=""):
        self.lbl_val.setText(val_str)
        self.lbl_sub.setText(sub_str)


class FullTelemetryWorker(QThread):
    data_updated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_running = True
        
        # State tracking for last valid readings to suppress anomalous spikes
        self.last_valid_soc = None
        self.last_valid_load = None
        self.last_valid_grid_pwr = None
        self.last_valid_p1 = None
        self.last_valid_p2 = None

    def stop(self):
        self._is_running = False

    def read_single_reg(self, client, addr):
        try:
            res = client.read_holding_registers(address=addr, count=1, slave=SLAVE)
            if (
                res.isError()
                or not hasattr(res, "registers")
                or len(res.registers) == 0
            ):
                return None
            return res.registers[0]
        except Exception:
            return None

    def parse_int16(self, val):
        if val is None:
            return 0
        return val - 65536 if val > 32767 else val

    def run(self):
        while self._is_running:
            try:
                client = ModbusTcpClient(IP, port=PORT, timeout=2)
                if not client.connect():
                    if self._is_running:
                        self.data_updated.emit(
                            {"error": "Failed to connect to inverter"}
                        )
                    time.sleep(2)
                    continue

                addrs = [
                    4112,
                    4113,
                    4115,
                    4116,
                    4117,
                    4119,
                    4875,
                    4890,
                    4894,
                    8192,
                    8198,
                    8200,
                    8202,
                ]
                reg_vals = {}
                for addr in addrs:
                    if not self._is_running:
                        break
                    val = self.read_single_reg(client, addr)
                    reg_vals[addr] = val if val is not None else 0

                client.close()

                if not self._is_running:
                    break

                # Raw conversions
                v1 = reg_vals[4112] * 0.1
                a1 = reg_vals[4113] * 0.01
                p1_raw = reg_vals[4115] * 0.1

                v2 = reg_vals[4116] * 0.1
                a2 = reg_vals[4117] * 0.01
                p2_raw = reg_vals[4119] * 0.1

                house_load_raw = reg_vals[4875] * 0.1

                soc_raw = reg_vals[8192]
                batt_v = reg_vals[8198] * 0.1
                batt_a = self.parse_int16(reg_vals[8200]) * 0.01
                batt_pwr = self.parse_int16(reg_vals[8202]) * 0.1

                grid_v = reg_vals[4890] * 0.1
                grid_a = self.parse_int16(reg_vals[4894]) * 0.01
                grid_pwr_raw = grid_v * grid_a

                # ==========================================
                # STEP 1: SANITY VALIDATION & SPIKE FILTERS
                # ==========================================
                
                # 1. Battery SoC (Valid range: 0-100%, reject sudden 100% or 0% register dropouts)
                if self.last_valid_soc is not None and abs(soc_raw - self.last_valid_soc) > 30 and (soc_raw == 100 or soc_raw == 0):
                    soc = self.last_valid_soc
                elif 0 <= soc_raw <= 100:
                    soc = soc_raw
                    self.last_valid_soc = soc
                else:
                    soc = self.last_valid_soc if self.last_valid_soc is not None else 0

                # 2. Grid Power (Clamp unphysical CT current read glitches > 25,000 W)
                if abs(grid_pwr_raw) > 25000:
                    grid_pwr = self.last_valid_grid_pwr if self.last_valid_grid_pwr is not None else 0.0
                else:
                    grid_pwr = grid_pwr_raw
                    self.last_valid_grid_pwr = grid_pwr

                # 3. House Load (Clamp anomalous power spikes > 15,000 W)
                if house_load_raw > 15000:
                    house_load = self.last_valid_load if self.last_valid_load is not None else 0.0
                else:
                    house_load = house_load_raw
                    self.last_valid_load = house_load

                # 4. PV Strings Power (Clamp anomalous PV generation spikes > 10,000 W)
                if p1_raw > 10000:
                    p1 = self.last_valid_p1 if self.last_valid_p1 is not None else 0.0
                else:
                    p1 = p1_raw
                    self.last_valid_p1 = p1

                if p2_raw > 10000:
                    p2 = self.last_valid_p2 if self.last_valid_p2 is not None else 0.0
                else:
                    p2 = p2_raw
                    self.last_valid_p2 = p2

                total_pv_power = p1 + p2

                metrics = {
                    "v1": v1,
                    "a1": a1,
                    "p1": p1,
                    "v2": v2,
                    "a2": a2,
                    "p2": p2,
                    "total_pv": total_pv_power,
                    "house_load": house_load,
                    "soc": soc,
                    "batt_v": batt_v,
                    "batt_a": batt_a,
                    "batt_pwr": batt_pwr,
                    "grid_v": grid_v,
                    "grid_a": grid_a,
                    "grid_pwr": grid_pwr,
                    "error": None,
                }

                if self._is_running:
                    self.data_updated.emit(metrics)

            except Exception as e:
                if self._is_running:
                    self.data_updated.emit({"error": f"Worker Exception: {str(e)}"})

            for _ in range(10):
                if not self._is_running:
                    break
                time.sleep(0.1)


class DuraIDashboard(QWidget):

    def __init__(self):
        super().__init__()
        self.timestamps = deque(maxlen=WINDOW_SECONDS)

        # Telemetry Buffers
        self.pv1_hist = deque(maxlen=WINDOW_SECONDS)
        self.pv2_hist = deque(maxlen=WINDOW_SECONDS)
        self.load_hist = deque(maxlen=WINDOW_SECONDS)
        self.batt_pwr_hist = deque(maxlen=WINDOW_SECONDS)
        self.soc_hist = deque(maxlen=WINDOW_SECONDS)
        self.grid_pwr_hist = deque(maxlen=WINDOW_SECONDS)

        self.last_log_time = 0

        self.init_ui()

        self.worker = FullTelemetryWorker()
        self.worker.data_updated.connect(self.update_ui)
        self.worker.start()

    def init_ui(self):
        self.setWindowTitle("Duracell Dura-i Inverter Live Energy Dashboard")
        self.resize(1400, 900)
        self.setStyleSheet("background-color: #11111B;")

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # Header Title
        font_header = QFont("Sans-Serif", 13, QFont.Weight.Bold)
        header = QLabel("DURACELL DURA-I INVERTER TELEMETRY DASHBOARD")
        header.setFont(font_header)
        header.setStyleSheet("color: #CDD6F4;")
        main_layout.addWidget(header)

        # Metric Cards Header Row
        cards_layout = QGridLayout()
        cards_layout.setSpacing(8)

        self.card_pv1 = MetricCard("SOLAR STRING 1 (SE)", COLOR_PV1)
        self.card_pv2 = MetricCard("SOLAR STRING 2 (NW)", COLOR_PV2)
        self.card_load = MetricCard("HOUSE LOAD DEMAND", COLOR_LOAD)
        self.card_batt = MetricCard("BATTERY STATUS", COLOR_BATT)
        self.card_grid = MetricCard("GRID CT INTERACTION", COLOR_GRID)

        cards_layout.addWidget(self.card_pv1, 0, 0)
        cards_layout.addWidget(self.card_pv2, 0, 1)
        cards_layout.addWidget(self.card_load, 0, 2)
        cards_layout.addWidget(self.card_batt, 0, 3)
        cards_layout.addWidget(self.card_grid, 0, 4)

        main_layout.addLayout(cards_layout)

        # Global PyQtGraph Setup
        pg.setConfigOption("background", "#1E1E2E")
        pg.setConfigOption("foreground", "#CDD6F4")

        # CHART 1: TOTAL POWER BALANCE
        self.plot_power = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")},
            title=(
                "<b>1. Power Balance (Watts) — House Load vs PV Generation vs"
                " Battery</b>"
            ),
        )
        self.plot_power.showGrid(x=True, y=True, alpha=0.2)
        self.plot_power.addLegend(offset=(10, 10))
        self.plot_power.setLabel("left", "Power (W)")

        self.curve_load = self.plot_power.plot(
            pen=pg.mkPen(COLOR_LOAD, width=2.5), name="House Load Demand (R4875)"
        )
        self.curve_pv1 = self.plot_power.plot(
            pen=pg.mkPen(COLOR_PV1, width=2), name="PV String 1 (R4115)"
        )
        self.curve_pv2 = self.plot_power.plot(
            pen=pg.mkPen(COLOR_PV2, width=2), name="PV String 2 (R4119)"
        )
        self.curve_batt_pwr = self.plot_power.plot(
            pen=pg.mkPen(COLOR_BATT, width=2),
            name="Battery Discharge(+) / Charge(-) (R8202)",
        )

        # CHART 2: BATTERY STATE OF CHARGE
        self.plot_soc = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")},
            title="<b>2. Battery Capacity — State of Charge (%)</b>",
        )
        self.plot_soc.showGrid(x=True, y=True, alpha=0.2)
        self.plot_soc.setLabel("left", "SoC (%)")
        self.plot_soc.setYRange(0, 100)
        self.curve_soc = self.plot_soc.plot(
            pen=pg.mkPen(COLOR_BATT, width=2.5), name="Battery SoC (R8192)"
        )

        # CHART 3: GRID POWER & CT CURRENT
        self.plot_grid = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")},
            title=(
                "<b>3. Grid Clamp Interaction — AC Power Import/Export (W)</b>"
            ),
        )
        self.plot_grid.showGrid(x=True, y=True, alpha=0.2)
        self.plot_grid.setLabel("left", "Grid Power (W)")
        self.curve_grid_pwr = self.plot_grid.plot(
            pen=pg.mkPen(COLOR_GRID, width=2), name="Grid Power (Approx)"
        )

        main_layout.addWidget(self.plot_power)
        main_layout.addWidget(self.plot_soc)
        main_layout.addWidget(self.plot_grid)

        self.footer = QLabel("Initializing Sampler...")
        self.footer.setStyleSheet("color: #6C7086;")
        main_layout.addWidget(self.footer)

        self.setLayout(main_layout)

    def push_to_github(self, metrics):
        """Appends telemetry to JSON, overwrites GitHub commit, and prunes local packfiles."""
        try:
            now = time.time()
            cutoff = now - RETENTION_PERIOD

            # 1. Prepare new record with timestamp
            record = dict(metrics)
            record["timestamp"] = int(now)

            # 2. Load existing history array
            history = []
            if os.path.exists(JSON_FILE):
                try:
                    with open(JSON_FILE, "r") as f:
                        history = json.load(f)
                except Exception:
                    history = []

            if not isinstance(history, list):
                history = []

            # 3. Append new record & prune records older than 24 hours
            history.append(record)
            pruned_history = [
                entry
                for entry in history
                if entry.get("timestamp", 0) >= cutoff
            ]

            # 4. Write pruned JSON array
            with open(JSON_FILE, "w") as f:
                json.dump(pruned_history, f, indent=2)

            # 5. Stage the updated JSON file
            subprocess.run(
                ["git", "add", JSON_FILE], cwd=SCRIPT_DIR, capture_output=True
            )

            # 6. Amend existing commit instead of making a new one
            commit_res = subprocess.run(
                ["git", "commit", "--amend", "--no-edit"],
                cwd=SCRIPT_DIR,
                capture_output=True,
            )
            if commit_res.returncode != 0:
                # Fallback if no initial commit exists yet
                subprocess.run(
                    ["git", "commit", "-m", "Telemetry live sync"],
                    cwd=SCRIPT_DIR,
                    capture_output=True,
                )

            # 7. Force-push to overwrite remote main branch with single commit
            subprocess.run(
                ["git", "push", "-f", "origin", "main"],
                cwd=SCRIPT_DIR,
                capture_output=True,
            )

            # 8. Expire reflog and prune loose objects/packfiles locally
            subprocess.run(
                ["git", "reflog", "expire", "--expire=now", "--all"],
                cwd=SCRIPT_DIR,
                capture_output=True,
            )
            subprocess.run(
                ["git", "gc", "--prune=now"],
                cwd=SCRIPT_DIR,
                capture_output=True,
            )

        except Exception as e:
            print("GitHub Sync Error:", e)

    def update_ui(self, m):
        if m.get("error"):
            self.footer.setText(f"Status Notice: {m['error']}")
            return

        now_ts = time.time()

        # Update Card Metrics
        self.card_pv1.update_data(
            f"{m['p1']:.1f} W", f"{m['v1']:.1f} V  |  {m['a1']:.2f} A"
        )
        self.card_pv2.update_data(
            f"{m['p2']:.1f} W", f"{m['v2']:.1f} V  |  {m['a2']:.2f} A"
        )
        self.card_load.update_data(
            f"{m['house_load']:.1f} W", "Real-Time Demand"
        )

        batt_state = (
            "Discharging"
            if m["batt_pwr"] > 50
            else ("Charging" if m["batt_pwr"] < -50 else "Idle")
        )
        self.card_batt.update_data(
            f"{m['soc']}% ({m['batt_pwr']:.0f} W)",
            f"{batt_state}  |  {m['batt_v']:.1f} V",
        )

        grid_state = (
            "Importing"
            if m["grid_pwr"] > 50
            else ("Exporting" if m["grid_pwr"] < -50 else "Neutral")
        )
        self.card_grid.update_data(
            f"{m['grid_pwr']:.0f} W", f"{grid_state}  |  {m['grid_v']:.1f} V"
        )

        # Append Data
        self.timestamps.append(now_ts)
        self.pv1_hist.append(m["p1"])
        self.pv2_hist.append(m["p2"])
        self.load_hist.append(m["house_load"])
        self.batt_pwr_hist.append(m["batt_pwr"])
        self.soc_hist.append(m["soc"])
        self.grid_pwr_hist.append(m["grid_pwr"])

        x = list(self.timestamps)

        # Update Plots
        self.curve_load.setData(x, list(self.load_hist))
        self.curve_pv1.setData(x, list(self.pv1_hist))
        self.curve_pv2.setData(x, list(self.pv2_hist))
        self.curve_batt_pwr.setData(x, list(self.batt_pwr_hist))

        self.curve_soc.setData(x, list(self.soc_hist))
        self.curve_grid_pwr.setData(x, list(self.grid_pwr_hist))

        # Synchronize Time Window (Scrolling)
        window_start = (
            max(x[0], now_ts - WINDOW_SECONDS) if x else now_ts - 3600
        )
        self.plot_power.setXRange(window_start, now_ts, padding=0)
        self.plot_soc.setXRange(window_start, now_ts, padding=0)
        self.plot_grid.setXRange(window_start, now_ts, padding=0)

        # Logging & GitHub Push (Every 60 seconds)
        if now_ts - self.last_log_time >= 60:
            file_exists = False
            try:
                with open(LOG_FILE, "r") as f:
                    file_exists = True
            except FileNotFoundError:
                pass

            with open(LOG_FILE, mode="a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "Timestamp",
                        "PV1_V",
                        "PV1_A",
                        "PV1_W",
                        "PV2_V",
                        "PV2_A",
                        "PV2_W",
                        "House_Load_W",
                        "Batt_SoC_%",
                        "Batt_V",
                        "Batt_A",
                        "Batt_W",
                        "Grid_V",
                        "Grid_A",
                        "Grid_W",
                    ])
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    f"{m['v1']:.1f}",
                    f"{m['a1']:.2f}",
                    f"{m['p1']:.1f}",
                    f"{m['v2']:.1f}",
                    f"{m['a2']:.2f}",
                    f"{m['p2']:.1f}",
                    f"{m['house_load']:.1f}",
                    f"{m['soc']}",
                    f"{m['batt_v']:.1f}",
                    f"{m['batt_a']:.2f}",
                    f"{m['batt_pwr']:.1f}",
                    f"{m['grid_v']:.1f}",
                    f"{m['grid_a']:.2f}",
                    f"{m['grid_pwr']:.1f}",
                ])

            # Trigger GitHub Push
            self.push_to_github(m)

            self.last_log_time = now_ts

        last_str = (
            datetime.fromtimestamp(self.last_log_time).strftime("%H:%M:%S")
            if self.last_log_time
            else "Pending"
        )
        self.footer.setText(
            f"Connected: {IP}:{PORT} • Logging & Syncing to GitHub • Last Sync:"
            f" {last_str}"
        )

    def closeEvent(self, event):
        self.worker.stop()
        self.worker.wait()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DuraIDashboard()
    window.show()
    sys.exit(app.exec())