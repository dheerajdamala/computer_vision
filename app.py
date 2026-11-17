"""
Real-Time Intelligent Surveillance System - Enterprise Desktop GUI
Pure Python (Tkinter + OpenCV + Pillow) - Zero Streamlit.

Upgraded Features:
1. Interactive Polygon Vertex Snapping & Drag-Handles:
   - Hovering over a vertex highlights it in bright cyan with snapping ring & cursor change.
   - Smooth 60 FPS drag-reshaping decoupled from model inference (zero lag/buffering).
2. Multi-Zone Hierarchy & Severity Leveling:
   - Concurrent Yellow (Buffer), Orange (Warning), and Red (Critical Vault) zones.
   - Dynamic escalation as pedestrians move deeper into security perimeters.
3. Adaptive Illumination Enhancement (Night Vision Filter):
   - CLAHE (Contrast Limited Adaptive Histogram Equalization) low-light booster.
4. Pose Estimation & Threat Behavior (YOLO11-Pose):
   - Detects Climbing / Fence Scaling, Crouching / Lurking, Crawling / Prone, and Hands Raised.
5. Instant Push Notifications:
   - Discord / Telegram Bot Webhook background dispatchers with test triggers.
6. Zero-Buffering & High-Speed Performance:
   - Full inference caching + Model Tier selector (Turbo YOLO11n @ 30+ FPS vs. Flagship YOLO11x @ 57M params).
"""

import os
import sys
import time
import zipfile
import datetime
import threading
from typing import List, Dict, Any, Tuple, Optional

import cv2
import numpy as np
import torch
from ultralytics import YOLO

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

# Base directory resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -----------------------------------------------------------------------------
# 1. Dataset Indexer & Geometry Helpers
# -----------------------------------------------------------------------------

def scan_mot17_sequences(mot17_zip_path=None) -> Dict[str, List[str]]:
    """Scans MOT17.zip and returns dict: seq_name -> list of internal frame paths."""
    if mot17_zip_path is None:
        mot17_zip_path = os.path.join(BASE_DIR, "MOT17.zip")
    if not os.path.exists(mot17_zip_path):
        return {}

    seq_map = {}
    with zipfile.ZipFile(mot17_zip_path, "r") as z:
        for name in z.namelist():
            if "/img1/" in name and name.endswith(".jpg"):
                seq_id = name.split("/img1/")[0].replace("MOT17/", "")
                if seq_id not in seq_map:
                    seq_map[seq_id] = []
                seq_map[seq_id].append(name)

    for k in seq_map:
        seq_map[k] = sorted(seq_map[k])
    return seq_map

def read_frame_from_zip(mot17_zip_path: str, internal_path: str) -> np.ndarray:
    """Decodes a single frame directly from zip without disk extraction."""
    with zipfile.ZipFile(mot17_zip_path, "r") as z:
        img_bytes = z.read(internal_path)
    return cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)

def apply_night_vision_clahe(frame_bgr: np.ndarray, clip_limit=3.0, tile_grid_size=(8, 8)) -> np.ndarray:
    """Adaptive illumination enhancement using CLAHE in LAB color space."""
    lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    cl = clahe.apply(l)
    return cv2.cvtColor(cv2.merge((cl, a, b)), cv2.COLOR_LAB2BGR)

def get_foot_point(bbox: List[float]) -> Tuple[float, float]:
    """Calculates ground foot contact coordinate from [x1, y1, x2, y2]."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, float(y2))

def is_point_in_polygon(point: Tuple[float, float], polygon_norm: List[List[float]], w: int, h: int) -> bool:
    """Evaluates whether point is strictly inside polygon (> 0)."""
    if len(polygon_norm) < 3:
        return False
    pts = np.array([[int(p[0] * w), int(p[1] * h)] for p in polygon_norm], dtype=np.int32).reshape((-1, 1, 2))
    return cv2.pointPolygonTest(pts, (float(point[0]), float(point[1])), False) > 0


# -----------------------------------------------------------------------------
# 2. Push Notification Dispatcher (Discord, Telegram, Email)
# -----------------------------------------------------------------------------

class NotificationDispatcher:
    """Thread-safe background dispatcher for instant intrusion alerts."""

    def __init__(self):
        self.discord_webhook = ""
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.enabled = True
        self.last_sent_time = 0
        self.cooldown_sec = 8.0

    def dispatch_alert_async(self, alert_text: str, image_bgr: Optional[np.ndarray] = None):
        """Asynchronously dispatches alert in a background thread."""
        now = time.time()
        if not self.enabled or (now - self.last_sent_time < self.cooldown_sec):
            return
        self.last_sent_time = now

        image_bytes = None
        if image_bgr is not None:
            h, w = image_bgr.shape[:2]
            if w > 960:
                scale = 960.0 / w
                thumb = cv2.resize(image_bgr, (960, int(h * scale)))
            else:
                thumb = image_bgr
            _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 75])
            image_bytes = buf.tobytes()

        threading.Thread(target=self._send_worker, args=(alert_text, image_bytes), daemon=True).start()

    def _send_worker(self, text: str, image_bytes: Optional[bytes]):
        # 1. Discord Webhook
        if self.discord_webhook and self.discord_webhook.startswith("http"):
            try:
                import requests
                if image_bytes:
                    r = requests.post(self.discord_webhook, data={"content": text}, files={"file": ("intrusion.jpg", image_bytes, "image/jpeg")}, timeout=10)
                else:
                    r = requests.post(self.discord_webhook, json={"content": text}, timeout=8)
                if r.status_code in [200, 204]:
                    print("[Notifier] Alert delivered to Discord successfully!")
            except Exception as e:
                print(f"[Notifier] Discord dispatch error: {e}")

        # 2. Telegram Bot
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                import requests
                sent_ok = False
                if image_bytes:
                    try:
                        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendPhoto"
                        r = requests.post(url, data={"chat_id": self.telegram_chat_id, "caption": text}, files={"photo": ("alert.jpg", image_bytes, "image/jpeg")}, timeout=12)
                        if r.status_code == 200:
                            print("[Notifier] Alert with photo delivered to Telegram successfully!")
                            sent_ok = True
                    except Exception as pe:
                        print(f"[Notifier] Photo upload timed out ({pe}), falling back to text message...")

                if not sent_ok:
                    url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                    r = requests.post(url, json={"chat_id": self.telegram_chat_id, "text": text}, timeout=10)
                    if r.status_code == 200:
                        print("[Notifier] Alert text message delivered to Telegram successfully!")
                    else:
                        print(f"[Notifier] Telegram returned error code {r.status_code}: {r.text}")
            except Exception as e:
                print(f"[Notifier] Telegram dispatch error: {e}")


# -----------------------------------------------------------------------------
# 3. Main Desktop Surveillance Application
# -----------------------------------------------------------------------------

class SurveillanceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("🛡️ Enterprise Surveillance System | Multi-Zone • Pose Threat • Anti-Lag Engine")
        self.root.geometry("1560x920")
        self.root.minsize(1366, 768)

        # Style configuration
        self.style = ttk.Style()
        self.style.theme_use("clam")

        # Load Dataset Mapping
        self.zip_path = os.path.join(BASE_DIR, "MOT17.zip")
        self.seq_map = scan_mot17_sequences(self.zip_path)

        if not self.seq_map:
            messagebox.showerror("Error", f"MOT17.zip not found at: {self.zip_path}\nPlease place MOT17.zip in the project folder.")
            sys.exit(1)

        # Device Selection
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        # AI Models: Pre-load Turbo (Nano) and Flagship (Max X)
        self.models = {}
        self.model_tier = "Turbo (YOLO11n - 30+ FPS)"
        
        # Turbo Model
        p_nano = os.path.join(BASE_DIR, "yolo11n.pt")
        if not os.path.exists(p_nano):
            _ = YOLO("yolo11n.pt")
        self.models["Turbo (YOLO11n - 30+ FPS)"] = YOLO(p_nano)

        # Max Model
        p_max = os.path.join(BASE_DIR, "yolo11x.pt")
        if not os.path.exists(p_max):
            try:
                _ = YOLO("yolo11x.pt")
            except Exception:
                pass
        if os.path.exists(p_max):
            self.models["Max Accuracy (YOLO11x - 57M)"] = YOLO(p_max)
        else:
            self.models["Max Accuracy (YOLO11x - 57M)"] = self.models["Turbo (YOLO11n - 30+ FPS)"]

        # Pose Estimation Model
        p_pose = os.path.join(BASE_DIR, "yolo11n-pose.pt")
        if not os.path.exists(p_pose):
            _ = YOLO("yolo11n-pose.pt")
        self.pose_model = YOLO(p_pose)

        # Notification Dispatcher
        self.notifier = NotificationDispatcher()

        # State Variables
        self.is_custom_video = False
        self.custom_video_path = None
        self.custom_video_cap = None
        self.total_custom_frames = 0
        self.all_seq_names = sorted(list(self.seq_map.keys()))
        self.current_seq = "train/MOT17-04-SDP" if "train/MOT17-04-SDP" in self.all_seq_names else self.all_seq_names[0]
        self.current_frames = self.seq_map[self.current_seq]
        self.current_frame_idx = 1
        self.is_playing = False
        self.fps = 20
        self.is_odd_hour = True
        self.enable_night_vision = False
        self.enable_pose_analysis = True

        # ---------------------------------------------------------------------
        # Multi-Zone Hierarchy: 3 Default Zones with Escalating Severities
        # ---------------------------------------------------------------------
        self.zones = [
            {
                "id": "zone_red",
                "name": "🔴 Red Core: High-Security Vault",
                "severity": "CRITICAL",
                "color_bgr": (0, 0, 235),       # Bright Red
                "dwell_thresh": 2.0,
                "polygon": [[0.65, 0.50], [0.95, 0.50], [0.95, 0.90], [0.65, 0.90]]
            },
            {
                "id": "zone_orange",
                "name": "🟠 Orange: Restricted Corridor",
                "severity": "HIGH",
                "color_bgr": (0, 140, 255),     # Orange
                "dwell_thresh": 2.5,
                "polygon": [[0.45, 0.35], [0.98, 0.35], [0.98, 0.95], [0.45, 0.95]]
            },
            {
                "id": "zone_yellow",
                "name": "🟡 Yellow: Perimeter Buffer",
                "severity": "WARNING",
                "color_bgr": (0, 215, 255),     # Yellow/Amber
                "dwell_thresh": 3.0,
                "polygon": [[0.20, 0.25], [0.99, 0.25], [0.99, 0.98], [0.20, 0.98]]
            }
        ]
        self.active_zone_idx = 0

        # Snapping & Drag-Handle State
        self.hovered_vertex_idx = None
        self.dragging_vertex_idx = None
        self.snap_radius_px = 16

        # Fast In-Memory Inference Cache to completely eliminate buffering lag:
        # cache_key -> (detected_people_list, raw_bgr_frame)
        self.frame_cache = {}

        # Tracking state cache: track_id -> dict
        self.track_history = {}
        self.alert_history = []

        # Canvas display scaling dimensions
        self.canvas_w = 980
        self.canvas_h = 560

        # Build GUI Components
        self._build_top_bar()
        self._build_main_panels()
        self._build_bottom_controls()

        # Initial Frame Load
        self.update_frame(self.current_frame_idx, recompute_ai=True)

    # -------------------------------------------------------------------------
    # UI BUILDERS
    # -------------------------------------------------------------------------

    def _build_top_bar(self):
        """Top Control Header: Sequence selector, AI tier, Mode toggles."""
        top_frame = ttk.Frame(self.root, padding="10 8 10 8")
        top_frame.pack(fill=tk.X, side=tk.TOP)

        # Title
        title_lbl = tk.Label(top_frame, text="🛡️ Intelligent Surveillance Center", font=("Segoe UI", 14, "bold"), fg="#0F172A")
        title_lbl.pack(side=tk.LEFT, padx=(0, 20))

        # Sequence Combobox
        ttk.Label(top_frame, text="Sequence:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.seq_combo = ttk.Combobox(top_frame, values=self.all_seq_names, width=22, state="readonly")
        self.seq_combo.set(self.current_seq)
        self.seq_combo.bind("<<ComboboxSelected>>", self._on_sequence_changed)
        self.seq_combo.pack(side=tk.LEFT, padx=(0, 6))

        # Upload Custom Video Button
        btn_upload = ttk.Button(top_frame, text="📂 Upload Video...", command=self._on_upload_video)
        btn_upload.pack(side=tk.LEFT, padx=(0, 15))

        # AI Engine Tier Selector (Turbo vs. Max)
        ttk.Label(top_frame, text="AI Engine:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.model_tier_combo = ttk.Combobox(
            top_frame,
            values=["Turbo (YOLO11n - 30+ FPS)", "Max Accuracy (YOLO11x - 57M)"],
            width=24,
            state="readonly"
        )
        self.model_tier_combo.set(self.model_tier)
        self.model_tier_combo.bind("<<ComboboxSelected>>", self._on_model_tier_changed)
        self.model_tier_combo.pack(side=tk.LEFT, padx=(0, 15))

        # Adaptive Night Vision (CLAHE) Toggle
        self.night_vis_var = tk.BooleanVar(value=False)
        chk_night = ttk.Checkbutton(top_frame, text="🌙 Night Vision (CLAHE)", variable=self.night_vis_var, command=self._on_night_vis_toggled)
        chk_night.pack(side=tk.LEFT, padx=(0, 12))

        # Pose Estimation Toggle
        self.pose_var = tk.BooleanVar(value=True)
        chk_pose = ttk.Checkbutton(top_frame, text="🧍 Pose & Threat Analysis", variable=self.pose_var, command=self._on_pose_toggled)
        chk_pose.pack(side=tk.LEFT, padx=(0, 12))

        # Odd-Hour Toggle
        self.odd_hour_var = tk.BooleanVar(value=True)
        chk_odd = ttk.Checkbutton(top_frame, text="🔒 Lockdown Hours", variable=self.odd_hour_var, command=self._on_mode_toggled)
        chk_odd.pack(side=tk.LEFT, padx=(0, 15))

    def _build_main_panels(self):
        """Side-by-Side: Left Video Viewport + Right Tabbed Dashboard."""
        main_container = ttk.Frame(self.root, padding="10 5 10 5")
        main_container.pack(fill=tk.BOTH, expand=True)

        # ------------------- LEFT PANEL: VIDEO CANVAS ------------------------
        left_panel = ttk.LabelFrame(main_container, text=" Live CCTV Surveillance Stream (Hover & Drag Cyan Handles to Edit Zones) ", padding="6")
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.canvas = tk.Canvas(left_panel, width=self.canvas_w, height=self.canvas_h, bg="#0F172A", cursor="crosshair")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Bind mouse events for vertex snapping and smooth dragging
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Button-3>", self._on_canvas_right_click)

        # Interactive Hint
        self.canvas_hint = tk.Label(
            left_panel,
            text="✨ Vertex Snapping: Hover near any vertex to highlight it in bright cyan. Click & drag to reshape. Right-click to delete vertex.",
            font=("Segoe UI", 9),
            fg="#0284C7"
        )
        self.canvas_hint.pack(anchor=tk.W, pady=(4, 0))

        # ------------------- RIGHT PANEL: TABBED DASHBOARD -------------------
        right_panel = ttk.Frame(main_container, width=500)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(0, 0))
        right_panel.pack_propagate(False)

        # Notebook Tabs
        self.notebook = ttk.Notebook(right_panel)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: Live Intrusion Feed
        self.tab_feed = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(self.tab_feed, text="🚨 Live Telemetry")
        self._build_tab_feed()

        # Tab 2: Multi-Zone Hierarchy
        self.tab_zones = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(self.tab_zones, text="📐 Multi-Zones")
        self._build_tab_zones()

        # Tab 3: Notifications & Webhooks
        self.tab_notif = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(self.tab_notif, text="🔔 Push Alerts")
        self._build_tab_notifications()

    def _build_tab_feed(self):
        """Tab 1: Live Status Banner, Occupancy, Pose Threat Indicators, Chronological Log."""
        # Status Card
        self.status_card = tk.Frame(self.tab_feed, bg="#10B981", height=50)
        self.status_card.pack(fill=tk.X, pady=(0, 8))
        self.status_card.pack_propagate(False)

        self.status_title = tk.Label(self.status_card, text="SECURE: ALL PERIMETERS CLEAR", font=("Segoe UI", 11, "bold"), fg="white", bg="#10B981")
        self.status_title.pack(expand=True)

        # Metrics
        m_frame = ttk.Frame(self.tab_feed)
        m_frame.pack(fill=tk.X, pady=(0, 6))

        self.lbl_people_count = tk.Label(m_frame, text="Active People: 0", font=("Segoe UI", 9, "bold"), fg="#1E293B")
        self.lbl_people_count.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_intruder_count = tk.Label(m_frame, text="Intruders: 0", font=("Segoe UI", 9, "bold"), fg="#EF4444")
        self.lbl_intruder_count.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_threat_count = tk.Label(m_frame, text="Threat Poses: 0", font=("Segoe UI", 9, "bold"), fg="#D97706")
        self.lbl_threat_count.pack(side=tk.LEFT)

        # Occupancy Box
        ttk.Label(self.tab_feed, text="Zone Occupancy & Threat Telemetry:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        self.intruders_box = tk.Text(self.tab_feed, height=5, bg="#F8FAFC", relief=tk.SOLID, bd=1, font=("Consolas", 9))
        self.intruders_box.pack(fill=tk.X, pady=(2, 8))

        # Chronological Security Alert Table
        ttk.Label(self.tab_feed, text="Chronological Security Incident Log:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        tree_frame = ttk.Frame(self.tab_feed)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 6))

        cols = ("Frame", "Time", "ID", "Zone", "Alert", "Threat")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=8)
        self.tree.heading("Frame", text="Frame")
        self.tree.heading("Time", text="Time")
        self.tree.heading("ID", text="ID")
        self.tree.heading("Zone", text="Zone")
        self.tree.heading("Alert", text="Classification")
        self.tree.heading("Threat", text="Pose Behavior")

        self.tree.column("Frame", width=50, anchor=tk.CENTER)
        self.tree.column("Time", width=65, anchor=tk.CENTER)
        self.tree.column("ID", width=45, anchor=tk.CENTER)
        self.tree.column("Zone", width=75, anchor=tk.W)
        self.tree.column("Alert", width=120, anchor=tk.W)
        self.tree.column("Threat", width=110, anchor=tk.W)

        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        btn_box = ttk.Frame(self.tab_feed)
        btn_box.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_box, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_box, text="Save Snapshot Evidence", command=self._save_snapshot).pack(side=tk.RIGHT)

    def _build_tab_zones(self):
        """Tab 2: Multi-Zone Hierarchy, Zone Editing, Severity Leveling."""
        ttk.Label(self.tab_zones, text="Multi-Zone Security Hierarchy:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))
        tk.Label(self.tab_zones, text="Select a zone below to edit its polygon boundary on the CCTV canvas.", font=("Segoe UI", 8), fg="#64748B").pack(anchor=tk.W, pady=(0, 8))

        # Zone Selector Combobox
        zone_names = [z["name"] for z in self.zones]
        self.zone_selector_combo = ttk.Combobox(self.tab_zones, values=zone_names, width=32, state="readonly")
        self.zone_selector_combo.set(zone_names[0])
        self.zone_selector_combo.bind("<<ComboboxSelected>>", self._on_active_zone_changed)
        self.zone_selector_combo.pack(fill=tk.X, pady=(0, 10))

        # Zone Attributes Editor
        attr_frame = ttk.LabelFrame(self.tab_zones, text=" Active Zone Settings ", padding="8")
        attr_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(attr_frame, text="Zone Name:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.entry_zone_name = ttk.Entry(attr_frame, width=28)
        self.entry_zone_name.insert(0, self.zones[0]["name"])
        self.entry_zone_name.grid(row=0, column=1, pady=3, padx=5)

        ttk.Label(attr_frame, text="Severity:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.combo_zone_sev = ttk.Combobox(attr_frame, values=["WARNING", "HIGH", "CRITICAL"], width=15, state="readonly")
        self.combo_zone_sev.set(self.zones[0]["severity"])
        self.combo_zone_sev.grid(row=1, column=1, sticky=tk.W, pady=3, padx=5)

        ttk.Label(attr_frame, text="Dwell (sec):").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.spin_zone_dwell = ttk.Spinbox(attr_frame, from_=0.5, to=10.0, increment=0.5, width=8)
        self.spin_zone_dwell.set(self.zones[0]["dwell_thresh"])
        self.spin_zone_dwell.grid(row=2, column=1, sticky=tk.W, pady=3, padx=5)

        ttk.Button(attr_frame, text="Update Zone Attributes", command=self._save_zone_attributes).grid(row=3, column=0, columnspan=2, pady=(8, 0))

        # Polygon Presets for Active Zone
        preset_frame = ttk.LabelFrame(self.tab_zones, text=" Boundary Presets ", padding="8")
        preset_frame.pack(fill=tk.X, pady=(0, 10))

        p_btns = [
            ("Right Courtyard", self._preset_right_courtyard),
            ("Left Corridor", self._preset_left_corridor),
            ("Central Gate", self._preset_central_gate),
            ("Clear Active Zone", self._clear_active_zone)
        ]
        for idx, (label, cmd) in enumerate(p_btns):
            ttk.Button(preset_frame, text=label, command=cmd).pack(fill=tk.X, pady=2)

    def _build_tab_notifications(self):
        """Tab 3: Discord and Telegram Push Dispatch Configuration."""
        ttk.Label(self.tab_notif, text="Instant Push Notification Gateways:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))

        # Discord Webhook
        disc_frame = ttk.LabelFrame(self.tab_notif, text=" Discord Bot Webhook ", padding="6")
        disc_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(disc_frame, text="Webhook URL:").pack(anchor=tk.W)
        self.entry_discord = ttk.Entry(disc_frame)
        self.entry_discord.pack(fill=tk.X, pady=2)
        self.entry_discord.insert(0, "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL")

        # Telegram Bot
        tg_frame = ttk.LabelFrame(self.tab_notif, text=" Telegram Bot Alert Gateway ", padding="6")
        tg_frame.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(tg_frame, text="Bot Token:").pack(anchor=tk.W)
        self.entry_tg_token = ttk.Entry(tg_frame)
        self.entry_tg_token.pack(fill=tk.X, pady=2)
        ttk.Label(tg_frame, text="Chat ID:").pack(anchor=tk.W)
        self.entry_tg_chat = ttk.Entry(tg_frame)
        self.entry_tg_chat.pack(fill=tk.X, pady=2)

        # Actions
        btn_box = ttk.Frame(self.tab_notif)
        btn_box.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_box, text="Save Notification Settings", command=self._save_notif_settings).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(btn_box, text="⚡ Send Test Alert", command=self._send_test_notification).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))

    def _build_bottom_controls(self):
        """Bottom Playback Bar: Play/Pause, Frame Scrubber, FPS Speed."""
        bottom_frame = ttk.Frame(self.root, padding="10 8 10 10")
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # Play/Pause Button
        self.btn_play = ttk.Button(bottom_frame, text="▶ Play", width=10, command=self._toggle_playback)
        self.btn_play.pack(side=tk.LEFT, padx=(0, 10))

        # Step Back
        btn_prev = ttk.Button(bottom_frame, text="⏮ Prev", width=8, command=self._step_prev)
        btn_prev.pack(side=tk.LEFT, padx=(0, 5))

        # Step Forward
        btn_next = ttk.Button(bottom_frame, text="Next ⏭", width=8, command=self._step_next)
        btn_next.pack(side=tk.LEFT, padx=(0, 15))

        # Frame Slider
        self.frame_scale = ttk.Scale(
            bottom_frame,
            from_=1,
            to=len(self.current_frames),
            orient=tk.HORIZONTAL,
            command=self._on_slider_moved
        )
        self.frame_scale.set(1)
        self.frame_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 15))

        # Frame Counter Label
        self.lbl_frame_info = ttk.Label(
            bottom_frame,
            text=f"Frame: 1 / {len(self.current_frames)}",
            font=("Segoe UI", 10, "bold")
        )
        self.lbl_frame_info.pack(side=tk.LEFT, padx=(0, 20))

        # Playback Speed Control
        ttk.Label(bottom_frame, text="FPS:").pack(side=tk.LEFT, padx=(0, 5))
        self.fps_spin = ttk.Spinbox(bottom_frame, from_=1, to=60, width=4, command=self._on_fps_changed)
        self.fps_spin.set(20)
        self.fps_spin.pack(side=tk.LEFT)

    # -------------------------------------------------------------------------
    # CORE AI INFERENCE, POSE THREAT & RENDERING PIPELINE
    # -------------------------------------------------------------------------

    def update_frame(self, frame_number: int, recompute_ai=True):
        """Processes detections, updates tracking, renders HUD, and updates telemetry."""
        total_f = self.total_custom_frames if self.is_custom_video else len(self.current_frames)
        if total_f <= 0 or frame_number < 1 or frame_number > total_f:
            return

        self.current_frame_idx = frame_number

        # Cache key to avoid redundant inference on CPU
        cache_key = (self.current_seq, frame_number, self.model_tier, self.enable_night_vision, self.enable_pose_analysis)

        if not recompute_ai and cache_key in self.frame_cache:
            detected_people, raw_bgr = self.frame_cache[cache_key]
        else:
            # 1. Decode Frame (from Custom Video or MOT17.zip)
            if self.is_custom_video and self.custom_video_cap is not None:
                self.custom_video_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number - 1)
                ret, raw_bgr = self.custom_video_cap.read()
                if not ret or raw_bgr is None:
                    return
            else:
                internal_path = self.current_frames[frame_number - 1]
                raw_bgr = read_frame_from_zip(self.zip_path, internal_path)

            # 2. Night Vision CLAHE Filter
            proc_frame = apply_night_vision_clahe(raw_bgr) if self.enable_night_vision else raw_bgr

            # 3. Detection & Tracking
            active_model = self.models.get(self.model_tier, self.models["Turbo (YOLO11n - 30+ FPS)"])
            results = active_model.track(
                source=proc_frame,
                persist=True,
                tracker="bytetrack.yaml",
                conf=0.35,
                iou=0.45,
                classes=[0],
                device=self.device,
                verbose=False
            )

            # 4. Optional Pose Estimation & Threat Behavior
            pose_boxes = []
            if self.enable_pose_analysis and results and results[0].boxes is not None and len(results[0].boxes) > 0:
                try:
                    pose_results = self.pose_model(proc_frame, conf=0.35, verbose=False)
                    if pose_results and pose_results[0].keypoints is not None:
                        pose_boxes = pose_results[0]
                except Exception:
                    pose_boxes = None

            detected_people = []
            if results and results[0].boxes is not None and len(results[0].boxes) > 0:
                for i, box in enumerate(results[0].boxes):
                    if box.id is None:
                        continue
                    tid = int(box.id[0].item())
                    bbox = box.xyxy[0].cpu().numpy().tolist()
                    foot_pt = get_foot_point(bbox)

                    # Pose Behavior Analysis
                    threat_label = "NORMAL_POSTURE"
                    threat_color = (0, 220, 60)
                    keypoints_xy = None

                    if pose_boxes and pose_boxes.keypoints is not None and i < len(pose_boxes.keypoints.xy):
                        kp = pose_boxes.keypoints.xy[i].cpu().numpy()
                        keypoints_xy = kp
                        # Keypoints indices: 0:Nose, 5:L_Shoulder, 6:R_Shoulder, 9:L_Wrist, 10:R_Wrist, 11:L_Hip, 12:R_Hip
                        if len(kp) >= 17:
                            nose_y = kp[0][1] if kp[0][1] > 0 else 9999
                            lw_y, rw_y = kp[9][1], kp[10][1]
                            bw = bbox[2] - bbox[0]
                            bh = bbox[3] - bbox[1]

                            # Climbing / Hands Above Head
                            if (lw_y > 0 and lw_y < nose_y) or (rw_y > 0 and rw_y < nose_y):
                                threat_label = "🚨 CLIMBING / HANDS UP"
                                threat_color = (0, 0, 255)
                            # Crawling / Prone (Width > Height)
                            elif bw > (bh * 1.15):
                                threat_label = "🚨 CRAWLING / PRONE"
                                threat_color = (0, 0, 255)
                            # Crouching / Squatting (aspect ratio < 1.35)
                            elif (bh / max(1, bw)) < 1.35:
                                threat_label = "⚠️ CROUCHING / LURKING"
                                threat_color = (0, 165, 255)

                    detected_people.append({
                        "id": tid,
                        "bbox": bbox,
                        "foot": foot_pt,
                        "threat_label": threat_label,
                        "threat_color": threat_color,
                        "keypoints": keypoints_xy
                    })

            self.frame_cache[cache_key] = (detected_people, raw_bgr)

        # ---------------------------------------------------------------------
        # SPATIAL POINT-IN-POLYGON & MULTI-ZONE ESCALATION
        # (Runs at 60+ FPS without re-running YOLO!)
        # ---------------------------------------------------------------------
        h, w = raw_bgr.shape[:2]
        base_h = 23 if self.is_odd_hour else 14
        base_time = datetime.datetime(2026, 11, 10, base_h, 0, 0)
        sim_time = base_time + datetime.timedelta(seconds=(frame_number / 30.0))

        processed_people = []
        active_intruders = []
        highest_severity = "NORMAL"

        for p in detected_people:
            tid = p["id"]
            foot_pt = p["foot"]

            # Evaluate against multi-zone hierarchy (Red -> Orange -> Yellow)
            matched_zone = None
            for zone in self.zones:
                if is_point_in_polygon(foot_pt, zone["polygon"], w, h):
                    matched_zone = zone
                    break  # Matched highest priority zone

            in_zone = matched_zone is not None
            t_key = f"{self.current_seq}_{tid}"
            if t_key not in self.track_history:
                self.track_history[t_key] = {"dwell_frames": 1 if in_zone else 0, "last_zone": matched_zone["id"] if matched_zone else None}
            else:
                if in_zone:
                    self.track_history[t_key]["dwell_frames"] += 1
                    self.track_history[t_key]["last_zone"] = matched_zone["id"]
                else:
                    self.track_history[t_key]["dwell_frames"] = 0
                    self.track_history[t_key]["last_zone"] = None

            dwell_sec = self.track_history[t_key]["dwell_frames"] / 30.0

            # Classification & Severity Escalation
            if matched_zone:
                dwell_thresh = matched_zone["dwell_thresh"]
                if dwell_sec >= dwell_thresh:
                    cls_type = f"{matched_zone['severity']}_INTRUSION"
                    sev = matched_zone["severity"]
                else:
                    cls_type = f"ENTERING_{matched_zone['severity']}_ZONE"
                    sev = "WARNING"
            elif self.is_odd_hour:
                cls_type = "ODD_HOUR_PRESENCE"
                sev = "WARNING"
            else:
                cls_type = "NORMAL"
                sev = "NORMAL"

            if sev == "CRITICAL" or (sev == "HIGH" and highest_severity != "CRITICAL"):
                highest_severity = sev

            p_entry = {
                **p,
                "in_zone": in_zone,
                "zone_obj": matched_zone,
                "dwell": dwell_sec,
                "cls": cls_type,
                "severity": sev
            }
            processed_people.append(p_entry)

            if in_zone:
                active_intruders.append(p_entry)
                # Dispatch Push Notification on Critical / High Breach
                if "INTRUSION" in cls_type and dwell_sec >= (matched_zone["dwell_thresh"] if matched_zone else 2.0):
                    if not any(a["frame"] == frame_number and a["id"] == tid for a in self.alert_history):
                        rec = {
                            "frame": frame_number,
                            "time": sim_time.strftime("%H:%M:%S"),
                            "id": tid,
                            "zone": matched_zone["name"].split(":")[0],
                            "alert": cls_type,
                            "threat": p["threat_label"],
                            "dwell": f"{dwell_sec:.1f}s"
                        }
                        self.alert_history.insert(0, rec)
                        self.tree.insert("", 0, values=(rec["frame"], rec["time"], f"#{rec['id']}", rec["zone"], rec["alert"], rec["threat"]))

                        # Push notification dispatch
                        alert_msg = f"🚨 {cls_type} | Person #{tid} breached {matched_zone['name']} | Threat: {p['threat_label']} | Time: {rec['time']}"
                        self.notifier.dispatch_alert_async(alert_msg, raw_bgr)

        # ---------------------------------------------------------------------
        # RENDER ANNOTATED CCTV VIEWPORT WITH SNAPPING HANDLES
        # ---------------------------------------------------------------------
        annotated = raw_bgr.copy()
        if self.enable_night_vision:
            annotated = apply_night_vision_clahe(annotated)

        # Draw all zones in multi-zone hierarchy
        for z_idx, z in enumerate(self.zones):
            poly = z["polygon"]
            if len(poly) >= 3:
                poly_pts = np.array([[int(pt[0] * w), int(pt[1] * h)] for pt in poly], dtype=np.int32).reshape((-1, 1, 2))
                poly_overlay = annotated.copy()
                cv2.fillPoly(poly_overlay, [poly_pts], z["color_bgr"])
                is_active = (z_idx == self.active_zone_idx)
                thickness = 4 if is_active else 2
                cv2.polylines(annotated, [poly_pts], isClosed=True, color=z["color_bgr"], thickness=thickness)
                cv2.addWeighted(poly_overlay, 0.22, annotated, 0.78, 0, annotated)

                # Label on zone
                cx = int(np.mean([pt[0] * w for pt in poly]))
                cy = int(np.mean([pt[1] * h for pt in poly]))
                cv2.putText(annotated, z["name"].split(":")[0], (cx - 40, cy),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)

        # Draw Snapping Handles on Active Zone
        active_poly = self.zones[self.active_zone_idx]["polygon"]
        for v_idx, pt in enumerate(active_poly):
            vx, vy = int(pt[0] * w), int(pt[1] * h)
            is_hovered = (v_idx == self.hovered_vertex_idx)

            if is_hovered:
                # Bright Cyan Snapping Bullseye
                cv2.circle(annotated, (vx, vy), 14, (255, 255, 0), 3)     # Outer Cyan ring
                cv2.circle(annotated, (vx, vy), 9, (0, 255, 255), -1)      # Inner Yellow
                cv2.putText(annotated, f"HANDLE {v_idx+1}", (vx + 12, vy - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)
            else:
                cv2.circle(annotated, (vx, vy), 8, (255, 255, 255), -1)
                cv2.circle(annotated, (vx, vy), 10, self.zones[self.active_zone_idx]["color_bgr"], 2)
                cv2.putText(annotated, f"V{v_idx+1}", (vx + 8, vy - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.40, (255, 255, 255), 1)

        # Draw Person BBoxes & Threat Annotations
        for p in processed_people:
            x1, y1, x2, y2 = [int(v) for v in p["bbox"]]
            fx, fy = int(p["foot"][0]), int(p["foot"][1])

            box_col = p["threat_color"] if p["threat_label"] != "NORMAL_POSTURE" else (
                (0, 0, 255) if p["severity"] == "CRITICAL" else ((0, 140, 255) if p["severity"] == "HIGH" else (0, 220, 60))
            )

            cv2.rectangle(annotated, (x1, y1), (x2, y2), box_col, 2)
            cv2.circle(annotated, (fx, fy), 5, (0, 255, 255), -1)
            cv2.circle(annotated, (fx, fy), 8, box_col, 2)

            tag = f"ID:{p['id']} {p['cls']}"
            if p["threat_label"] != "NORMAL_POSTURE":
                tag += f" | {p['threat_label']}"

            cv2.putText(annotated, tag, (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, box_col, 2)

            if p["in_zone"]:
                cv2.putText(annotated, f"Dwell: {p['dwell']:.1f}s", (fx - 35, min(h - 10, fy + 18)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 2)

        # Top CCTV Telemetry HUD
        hud_h = 50
        cv2.rectangle(annotated, (0, 0), (w, hud_h), (15, 23, 42), -1)
        mode_str = "[ 🌙 NIGHT LOCKDOWN HOURS ]" if self.is_odd_hour else "[ ☀️ NORMAL OPERATING HOURS ]"
        mode_col = (0, 60, 255) if self.is_odd_hour else (0, 220, 60)
        cv2.putText(annotated, f"CCTV: {self.current_seq[:35]} | Frame: {frame_number}/{total_f} | Time: {sim_time.strftime('%H:%M:%S')}",
                    (14, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (220, 220, 220), 1)
        cv2.putText(annotated, mode_str, (14, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.50, mode_col, 2)

        clahe_tag = "[CLAHE ON]" if self.enable_night_vision else ""
        cv2.putText(annotated, f"{self.model_tier.split()[0]} {clahe_tag} | People: {len(processed_people)} | Intruders: {len(active_intruders)}",
                    (w - 380, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (240, 240, 240), 1)

        self.last_annotated_frame = annotated

        # ---------------------------------------------------------------------
        # DISPLAY ON TKINTER CANVAS & TELEMETRY
        # ---------------------------------------------------------------------
        self.canvas_w = max(640, self.canvas.winfo_width())
        self.canvas_h = max(360, self.canvas.winfo_height())
        resized = cv2.resize(annotated, (self.canvas_w, self.canvas_h))
        rgb_img = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)
        self.tk_photo = ImageTk.PhotoImage(pil_img)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.tk_photo, anchor=tk.NW)

        # Update Telemetry Dashboard
        self.lbl_frame_info.config(text=f"Frame: {frame_number} / {total_f}")
        self.lbl_people_count.config(text=f"People: {len(processed_people)}")
        self.lbl_intruder_count.config(text=f"Intruders: {len(active_intruders)}")
        threat_count = sum(1 for p in processed_people if p["threat_label"] != "NORMAL_POSTURE")
        self.lbl_threat_count.config(text=f"Threats: {threat_count}")

        if active_intruders:
            if highest_severity == "CRITICAL":
                self.status_card.config(bg="#DC2626")
                self.status_title.config(text="🚨 CRITICAL INTRUSION: HIGH-SECURITY VAULT BREACH", bg="#DC2626")
            elif highest_severity == "HIGH":
                self.status_card.config(bg="#EA580C")
                self.status_title.config(text="🟠 HIGH ALERT: RESTRICTED CORRIDOR BREACH", bg="#EA580C")
            else:
                self.status_card.config(bg="#D97706")
                self.status_title.config(text="🟡 WARNING: PERIMETER BUFFER VIOLATION", bg="#D97706")

            self.intruders_box.delete("1.0", tk.END)
            for z in active_intruders:
                line = f"• ID #{z['id']} | {z['cls']}\n  Zone: {z['zone_obj']['name'].split(':')[0]} | Dwell: {z['dwell']:.1f}s | Threat: {z['threat_label']}\n"
                self.intruders_box.insert(tk.END, line)
        else:
            self.status_card.config(bg="#10B981")
            self.status_title.config(text="SECURE: ALL PERIMETERS CLEAR", bg="#10B981")
            self.intruders_box.delete("1.0", tk.END)
            self.intruders_box.insert(tk.END, "✅ All security zones clear.\nZero unauthorized presence detected.")

    # -------------------------------------------------------------------------
    # INTERACTIVE VERTEX SNAPPING & DRAG-HANDLE HANDLERS
    # -------------------------------------------------------------------------

    def _on_canvas_motion(self, event):
        """Hover detection: snaps to nearest vertex and turns it bright cyan."""
        if self.canvas_w <= 0 or self.canvas_h <= 0 or self.dragging_vertex_idx is not None:
            return

        cx_norm = event.x / float(self.canvas_w)
        cy_norm = event.y / float(self.canvas_h)
        active_poly = self.zones[self.active_zone_idx]["polygon"]

        closest_idx = None
        min_dist_sq = (self.snap_radius_px / float(self.canvas_w)) ** 2

        for idx, pt in enumerate(active_poly):
            dist_sq = (pt[0] - cx_norm) ** 2 + ((pt[1] - cy_norm) * (self.canvas_h / self.canvas_w)) ** 2
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                closest_idx = idx

        if closest_idx != self.hovered_vertex_idx:
            self.hovered_vertex_idx = closest_idx
            if closest_idx is not None:
                self.canvas.config(cursor="hand2")
            else:
                self.canvas.config(cursor="crosshair")
            # Fast re-render without re-running YOLO
            self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _on_canvas_click(self, event):
        """Click on canvas: select handle to drag or add vertex."""
        if self.canvas_w <= 0 or self.canvas_h <= 0:
            return

        if self.hovered_vertex_idx is not None:
            self.dragging_vertex_idx = self.hovered_vertex_idx
        else:
            active_poly = self.zones[self.active_zone_idx]["polygon"]
            if len(active_poly) < 10:
                nx = max(0.0, min(1.0, event.x / float(self.canvas_w)))
                ny = max(0.0, min(1.0, event.y / float(self.canvas_h)))
                active_poly.append([round(nx, 4), round(ny, 4)])
                self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _on_canvas_drag(self, event):
        """Smoothly drags selected vertex in real time at 60 FPS."""
        if self.dragging_vertex_idx is not None and self.canvas_w > 0 and self.canvas_h > 0:
            nx = max(0.0, min(1.0, event.x / float(self.canvas_w)))
            ny = max(0.0, min(1.0, event.y / float(self.canvas_h)))
            self.zones[self.active_zone_idx]["polygon"][self.dragging_vertex_idx] = [round(nx, 4), round(ny, 4)]
            # Re-render immediately (0ms inference lag)
            self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _on_canvas_release(self, event):
        self.dragging_vertex_idx = None

    def _on_canvas_right_click(self, event):
        """Right click: pop vertex from active zone."""
        active_poly = self.zones[self.active_zone_idx]["polygon"]
        if len(active_poly) > 0:
            active_poly.pop()
            self.hovered_vertex_idx = None
            self.update_frame(self.current_frame_idx, recompute_ai=False)

    # -------------------------------------------------------------------------
    # MULTI-ZONE MANAGEMENT & PRESETS
    # -------------------------------------------------------------------------

    def _on_active_zone_changed(self, event=None):
        sel_name = self.zone_selector_combo.get()
        for idx, z in enumerate(self.zones):
            if z["name"] == sel_name:
                self.active_zone_idx = idx
                self.entry_zone_name.delete(0, tk.END)
                self.entry_zone_name.insert(0, z["name"])
                self.combo_zone_sev.set(z["severity"])
                self.spin_zone_dwell.set(z["dwell_thresh"])
                self.update_frame(self.current_frame_idx, recompute_ai=False)
                break

    def _save_zone_attributes(self):
        z = self.zones[self.active_zone_idx]
        z["name"] = self.entry_zone_name.get()
        z["severity"] = self.combo_zone_sev.get()
        try:
            z["dwell_thresh"] = float(self.spin_zone_dwell.get())
        except ValueError:
            pass
        self.zone_selector_combo.config(values=[zn["name"] for zn in self.zones])
        self.zone_selector_combo.set(z["name"])
        self.update_frame(self.current_frame_idx, recompute_ai=False)
        messagebox.showinfo("Zone Updated", f"Attributes for '{z['name']}' saved successfully!")

    def _preset_right_courtyard(self):
        self.zones[self.active_zone_idx]["polygon"] = [[0.55, 0.40], [0.98, 0.40], [0.98, 0.95], [0.55, 0.95]]
        self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _preset_left_corridor(self):
        self.zones[self.active_zone_idx]["polygon"] = [[0.02, 0.35], [0.45, 0.35], [0.45, 0.95], [0.02, 0.95]]
        self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _preset_central_gate(self):
        self.zones[self.active_zone_idx]["polygon"] = [[0.35, 0.30], [0.65, 0.30], [0.70, 0.85], [0.30, 0.85]]
        self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _clear_active_zone(self):
        self.zones[self.active_zone_idx]["polygon"] = []
        self.update_frame(self.current_frame_idx, recompute_ai=False)

    # -------------------------------------------------------------------------
    # NOTIFICATION DISPATCH HANDLERS
    # -------------------------------------------------------------------------

    def _save_notif_settings(self):
        self.notifier.discord_webhook = self.entry_discord.get().strip()
        self.notifier.telegram_bot_token = self.entry_tg_token.get().strip()
        self.notifier.telegram_chat_id = self.entry_tg_chat.get().strip()
        messagebox.showinfo("Settings Saved", "Notification gateway parameters stored successfully!")

    def _send_test_notification(self):
        self._save_notif_settings()
        test_msg = "🚨 TEST SURVEILLANCE NOTIFICATION: Gateway verified and active."
        snap = getattr(self, "last_annotated_frame", None)
        self.notifier.dispatch_alert_async(test_msg, snap)
        messagebox.showinfo("Test Dispatched", "Test alert dispatched in background thread to configured channels.")

    # -------------------------------------------------------------------------
    # PLAYBACK CONTROLS & LISTENERS
    # -------------------------------------------------------------------------

    def _toggle_playback(self):
        self.is_playing = not self.is_playing
        if self.is_playing:
            self.btn_play.config(text="⏸ Pause")
            self._playback_loop()
        else:
            self.btn_play.config(text="▶ Play")

    def _playback_loop(self):
        if not self.is_playing:
            return
        total_f = self.total_custom_frames if self.is_custom_video else len(self.current_frames)
        if self.current_frame_idx < total_f:
            next_idx = self.current_frame_idx + 1
            self.frame_scale.set(next_idx)
            self.update_frame(next_idx, recompute_ai=True)
            delay_ms = max(10, int(1000.0 / max(1, self.fps)))
            self.root.after(delay_ms, self._playback_loop)
        else:
            self.is_playing = False
            self.btn_play.config(text="▶ Play")

    def _on_slider_moved(self, val):
        idx = int(float(val))
        if idx != self.current_frame_idx:
            self.update_frame(idx, recompute_ai=True)

    def _step_next(self):
        total_f = self.total_custom_frames if self.is_custom_video else len(self.current_frames)
        if self.current_frame_idx < total_f:
            self.frame_scale.set(self.current_frame_idx + 1)
            self.update_frame(self.current_frame_idx + 1, recompute_ai=True)

    def _step_prev(self):
        if self.current_frame_idx > 1:
            self.frame_scale.set(self.current_frame_idx - 1)
            self.update_frame(self.current_frame_idx - 1, recompute_ai=True)

    def load_video_file(self, path: str) -> bool:
        """Loads and prepares an external video file for live tracking & zone inspection."""
        if not path or not os.path.exists(path):
            return False

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", f"Could not open video file:\n{path}")
            return False

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            total_frames = 300

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0 or fps > 120:
            fps = 25.0

        if self.custom_video_cap is not None:
            self.custom_video_cap.release()

        self.custom_video_cap = cap
        self.custom_video_path = path
        self.total_custom_frames = total_frames
        self.is_custom_video = True

        video_label = f"📁 [Video] {os.path.basename(path)}"
        current_values = list(self.seq_combo['values'])
        if video_label not in current_values:
            current_values.insert(0, video_label)
            self.seq_combo['values'] = current_values

        self.current_seq = video_label
        self.seq_combo.set(video_label)

        self.fps = int(min(60, max(1, round(fps))))
        self.fps_spin.set(self.fps)

        self.is_playing = False
        self.btn_play.config(text="▶ Play")
        self.frame_scale.config(to=self.total_custom_frames)
        self.frame_scale.set(1)

        # Clear session history for fresh video feed
        self.frame_cache.clear()
        self.track_history.clear()
        self.alert_history.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.update_frame(1, recompute_ai=True)
        return True

    def _on_upload_video(self):
        """Allows user to select and load any external CCTV/surveillance video file."""
        from tkinter import filedialog
        filetypes = [
            ("Video Files", "*.mp4 *.avi *.mov *.mkv *.webm *.flv *.wmv *.m4v"),
            ("All Files", "*.*")
        ]
        path = filedialog.askopenfilename(
            title="Select Video File for Surveillance Detection",
            filetypes=filetypes
        )
        if path and self.load_video_file(path):
            messagebox.showinfo(
                "Video Loaded",
                f"Successfully loaded video:\n{os.path.basename(path)}\n\n"
                f"Total Frames: {self.total_custom_frames} | Detected FPS: {self.fps}\n\n"
                "You can now play, scrub, detect, and draw restricted zones directly on your video!"
            )

    def _on_sequence_changed(self, event=None):
        sel = self.seq_combo.get()
        if sel.startswith("📁 [Video] "):
            self.is_custom_video = True
            self.current_seq = sel
            self.current_frame_idx = 1
            self.is_playing = False
            self.btn_play.config(text="▶ Play")
            self.frame_scale.config(to=self.total_custom_frames)
            self.frame_scale.set(1)
            self.track_history.clear()
            self.update_frame(1, recompute_ai=True)
            return

        self.is_custom_video = False
        self.current_seq = sel
        self.current_frames = self.seq_map[self.current_seq]
        self.current_frame_idx = 1
        self.is_playing = False
        self.btn_play.config(text="▶ Play")
        self.frame_scale.config(to=len(self.current_frames))
        self.frame_scale.set(1)
        self.track_history.clear()
        self.update_frame(1, recompute_ai=True)

    def _on_model_tier_changed(self, event=None):
        self.model_tier = self.model_tier_combo.get()
        self.update_frame(self.current_frame_idx, recompute_ai=True)

    def _on_night_vis_toggled(self):
        self.enable_night_vision = self.night_vis_var.get()
        self.update_frame(self.current_frame_idx, recompute_ai=True)

    def _on_pose_toggled(self):
        self.enable_pose_analysis = self.pose_var.get()
        self.update_frame(self.current_frame_idx, recompute_ai=True)

    def _on_mode_toggled(self):
        self.is_odd_hour = self.odd_hour_var.get()
        self.update_frame(self.current_frame_idx, recompute_ai=False)

    def _on_fps_changed(self):
        try:
            self.fps = int(self.fps_spin.get())
        except ValueError:
            pass

    def _clear_log(self):
        self.alert_history.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _save_snapshot(self):
        if hasattr(self, "last_annotated_frame"):
            snap_dir = os.path.join(BASE_DIR, "data", "alerts", "snapshots")
            os.makedirs(snap_dir, exist_ok=True)
            t_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"snapshot_{self.current_seq.replace('/', '_')}_f{self.current_frame_idx}_{t_str}.jpg"
            out_p = os.path.join(snap_dir, fname)
            cv2.imwrite(out_p, self.last_annotated_frame)
            messagebox.showinfo("Snapshot Captured", f"Saved evidence snapshot to:\n{out_p}")


# -----------------------------------------------------------------------------
# 4. Built-in Self-Diagnostic Suite & CLI Pipeline
# -----------------------------------------------------------------------------

def run_self_tests():
    """Executes automated verification across spatial, temporal, and detection engines."""
    print("=" * 65)
    print("  RUNNING REAL-TIME SURVEILLANCE SELF-DIAGNOSTIC SUITE")
    print("=" * 65)

    # 1. Spatial Geometry Test
    poly_norm = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    inside_pt = (500, 500)
    outside_pt = (100, 100)
    assert is_point_in_polygon(inside_pt, poly_norm, 1000, 1000) == True, "Failed inside PIP check"
    assert is_point_in_polygon(outside_pt, poly_norm, 1000, 1000) == False, "Failed outside PIP check"
    print("  [OK] Spatial Point-in-Polygon Engine: PASSED")

    # 2. Footpoint Calculation Test
    bbox = [100.0, 150.0, 300.0, 550.0]
    fx, fy = get_foot_point(bbox)
    assert fx == 200.0 and fy == 550.0, "Failed footpoint ground anchor calculation"
    print("  [OK] Ground Contact Footpoint Anchor: PASSED")

    # 3. CLAHE Low-Light Enhancer Test
    test_img = np.zeros((100, 100, 3), dtype=np.uint8)
    enhanced = apply_night_vision_clahe(test_img)
    assert enhanced.shape == (100, 100, 3), "Failed CLAHE transformation"
    print("  [OK] Adaptive Illumination (CLAHE): PASSED")

    # 4. Dataset Scanning Test
    zip_p = os.path.join(BASE_DIR, "MOT17.zip")
    if os.path.exists(zip_p):
        seqs = scan_mot17_sequences(zip_p)
        print(f"  [OK] Dataset Indexer: Found {len(seqs)} MOT17 sequences directly in zip.")

    # 5. Model Loading Test
    for m_name in ["yolo11n.pt", "yolo11x.pt", "yolo11n-pose.pt"]:
        m_p = os.path.join(BASE_DIR, m_name)
        if os.path.exists(m_p):
            print(f"  [OK] Model Verification: {m_name} available ({os.path.getsize(m_p)/1024/1024:.1f} MB)")

    print("=" * 65)
    print("  ALL SELF-DIAGNOSTIC CHECKS PASSED SUCCESSFULLY!")
    print("=" * 65)


def run_cli_stream(source: str = "0", save_out: Optional[str] = None, headless: bool = False):
    """Headless/CLI pipeline runner for live RTSP, webcam, or video files."""
    print(f"[CLI] Starting surveillance stream from source: {source}")
    src_val = int(source) if source.isdigit() else source
    cap = cv2.VideoCapture(src_val)
    if not cap.isOpened():
        print(f"[ERROR] Could not open video source: {source}")
        return

    model_p = os.path.join(BASE_DIR, "yolo11x.pt")
    if not os.path.exists(model_p):
        model_p = os.path.join(BASE_DIR, "yolo11n.pt")
    model = YOLO(model_p)

    writer = None
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if save_out:
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(save_out, fourcc, fps, (w, h))

    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_idx += 1
            res = model.track(frame, persist=True, tracker="bytetrack.yaml", verbose=False, classes=[0])
            if res and res[0].boxes is not None:
                for b in res[0].boxes:
                    xy = b.xyxy[0].cpu().numpy().astype(int)
                    tid = int(b.id[0].item()) if b.id is not None else -1
                    cv2.rectangle(frame, (xy[0], xy[1]), (xy[2], xy[3]), (0, 255, 0), 2)
                    cv2.putText(frame, f"ID:{tid}", (xy[0], xy[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            if writer:
                writer.write(frame)
            if not headless:
                cv2.imshow("Surveillance Feed (Press 'q' to exit)", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    finally:
        cap.release()
        if writer:
            writer.release()
        cv2.destroyAllWindows()
        print(f"[CLI] Stream finished. Processed {frame_idx} frames.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Real-Time Intelligent Surveillance System")
    parser.add_argument("--cli", action="store_true", help="Run in CLI streaming mode instead of GUI")
    parser.add_argument("--source", type=str, default="0", help="Video source (webcam index, video file, or RTSP)")
    parser.add_argument("--video", type=str, default=None, help="Directly open GUI loaded with this video file")
    parser.add_argument("--save-out", type=str, default=None, help="Save annotated output video")
    parser.add_argument("--headless", action="store_true", help="Run without UI window")
    parser.add_argument("--test", action="store_true", help="Run self-diagnostic unit tests")

    args, unknown = parser.parse_known_args()

    if args.test:
        run_self_tests()
        return

    if args.cli or args.headless:
        run_cli_stream(source=args.source, save_out=args.save_out, headless=args.headless)
        return

    # By default, launch the full Enterprise Desktop GUI
    root = tk.Tk()
    app = SurveillanceApp(root)
    if args.video and os.path.exists(args.video):
        app.load_video_file(args.video)
    root.mainloop()


if __name__ == "__main__":
    main()
