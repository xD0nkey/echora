"""Echora - stream Windows system audio to AirPlay speakers (e.g. Sonos).

Captures the default output device via WASAPI loopback and streams it to
one or more AirPlay 2 receivers using pyatv (patched to send ALAC).
"""
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import winreg

import customtkinter as ctk
import pyatv
import pystray
from PIL import Image, ImageDraw
from pyatv.const import Protocol

import alac_patch

alac_patch.apply()  # Sonos needs ALAC, not raw PCM
from audio_engine import Capture, boost_timing

APP = "Echora"
APPDATA = os.environ.get("APPDATA", ".")
SETTINGS_DIR = os.path.join(APPDATA, APP)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
OLD_SETTINGS_FILE = os.path.join(APPDATA, "AirBlade", "settings.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LATENCY_MS = {"low": 500, "normal": 1000, "stable": 2000}

ACCENT = "#3B82F6"
CARD = ("#FFFFFF", "#1F2430")
CARD_ON = ("#E8F0FE", "#1E3A5F")
BG = ("#F2F4F7", "#141821")
MUTED = ("#6B7280", "#9CA3AF")

# ------------------------------------------------------------ translations
STRINGS = {
    "sv": {
        "scan": "⟳  Sök",
        "source": "Ljudkälla: {}",
        "speakers": "HÖGTALARE",
        "searching": "Söker efter AirPlay-högtalare…",
        "none_found": "Inga AirPlay-högtalare hittades.\nKontrollera att datorn är på samma nätverk.",
        "found": "{} högtalare hittade",
        "scan_error": "Sökfel: {}",
        "latency": "Fördröjning",
        "lat_low": "Låg", "lat_normal": "Normal", "lat_stable": "Stabil",
        "latency_set": "Fördröjning: {} ({:g} s)",
        "autostart": "Starta med Windows",
        "autostart_err": "Kunde inte ändra autostart: {}",
        "mute_local": "Tysta datorns högtalare/hörlurar vid streaming",
        "mute_err": "Kunde inte ändra ljudet lokalt: {}",
        "language": "Språk",
        "lang_auto": "Auto",
        "capture_err": "Kunde inte fånga systemljud: {}",
        "no_source": "Ingen ljudkälla",
        "connecting": "Ansluter…",
        "playing": "Spelar",
        "reconnecting": "Återansluter…",
        "tray_show": "Visa Echora",
        "tray_stop": "Stoppa all uppspelning",
        "tray_quit": "Avsluta",
        "tray_playing": "Echora – spelar på {}",
    },
    "en": {
        "scan": "⟳  Scan",
        "source": "Audio source: {}",
        "speakers": "SPEAKERS",
        "searching": "Looking for AirPlay speakers…",
        "none_found": "No AirPlay speakers found.\nMake sure this PC is on the same network.",
        "found": "{} speaker(s) found",
        "scan_error": "Scan error: {}",
        "latency": "Latency",
        "lat_low": "Low", "lat_normal": "Normal", "lat_stable": "Stable",
        "latency_set": "Latency: {} ({:g} s)",
        "autostart": "Start with Windows",
        "autostart_err": "Could not change autostart: {}",
        "mute_local": "Mute this PC's speakers/headphones while streaming",
        "mute_err": "Could not change local audio: {}",
        "language": "Language",
        "lang_auto": "Auto",
        "capture_err": "Could not capture system audio: {}",
        "no_source": "No audio source",
        "connecting": "Connecting…",
        "playing": "Playing",
        "reconnecting": "Reconnecting…",
        "tray_show": "Show Echora",
        "tray_stop": "Stop all playback",
        "tray_quit": "Quit",
        "tray_playing": "Echora – playing on {}",
    },
}
LANG_NAMES = {"auto": None, "sv": "Svenska", "en": "English"}


def windows_language() -> str:
    """'sv' if Windows' display language is Swedish, otherwise 'en'."""
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "sv" if (lang_id & 0x3FF) == 0x1D else "en"
    except Exception:
        return "en"


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    defaults = {"latency": "normal", "selected": [], "volumes": {},
                "autostart": False, "mute_local": True, "muted_by_us": False,
                "language": "auto"}
    path = SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else OLD_SETTINGS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            defaults.update(json.load(f))
    except Exception:
        pass
    # Migrate old (Swedish) latency names from AirBlade.
    defaults["latency"] = {"Låg": "low", "Normal": "normal", "Stabil": "stable"}.get(
        defaults["latency"], defaults["latency"])
    if defaults["latency"] not in LATENCY_MS:
        defaults["latency"] = "normal"
    return defaults


def save_settings(s: dict) -> None:
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def launch_args(*extra) -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return [pyw, os.path.abspath(__file__), *extra]


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        try:
            winreg.DeleteValue(k, "AirBlade")  # old name
        except FileNotFoundError:
            pass
        if enabled:
            cmd = subprocess.list2cmdline(launch_args("--minimized"))
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------- UI
class SpeakerCard(ctk.CTkFrame):
    def __init__(self, master, app, ident, conf):
        super().__init__(master, fg_color=CARD, corner_radius=14)
        self.app, self.ident = app, ident
        self.grid_columnconfigure(1, weight=1)

        self.icon = ctk.CTkLabel(self, text="🔈", font=ctk.CTkFont(size=22), width=40)
        self.icon.grid(row=0, column=0, rowspan=2, padx=(14, 6), pady=12)
        ctk.CTkLabel(self, text=conf.name, anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=1, sticky="sw", pady=(12, 0))
        self.sub = ctk.CTkLabel(self, text=self._base_text(), anchor="w",
                                text_color=MUTED, font=ctk.CTkFont(size=12))
        self.sub.grid(row=1, column=1, sticky="nw", pady=(0, 10))

        self.switch_var = ctk.BooleanVar(value=False)
        self.switch = ctk.CTkSwitch(self, text="", variable=self.switch_var, width=46,
                                    progress_color=ACCENT, command=self.on_toggle)
        self.switch.grid(row=0, column=2, rowspan=2, padx=14)

        vol = app.settings["volumes"].get(ident, 40)
        self.vol_row = ctk.CTkFrame(self, fg_color="transparent")
        self.vol_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.vol_row, text="🔊", width=24).grid(row=0, column=0, padx=(14, 4))
        self.slider = ctk.CTkSlider(self.vol_row, from_=0, to=100, button_color=ACCENT,
                                    progress_color=ACCENT, command=self.on_volume)
        self.slider.set(vol)
        self.slider.grid(row=0, column=1, sticky="ew")
        self.vol_label = ctk.CTkLabel(self.vol_row, text=f"{int(vol)}", width=34, text_color=MUTED)
        self.vol_label.grid(row=0, column=2, padx=(6, 14))
        self._vol_job = None

    def _base_text(self):
        conf = self.app.devices[self.ident]
        model = getattr(conf.device_info, "raw_model", None) or ""
        return f"{model}  ·  {conf.address}".strip(" ·")

    def on_toggle(self):
        if self.switch_var.get():
            self.app.start(self.ident)
        else:
            self.app.stop(self.ident, user=True)

    def on_volume(self, v):
        self.vol_label.configure(text=f"{int(v)}")
        self.app.settings["volumes"][self.ident] = int(v)
        if self._vol_job:
            self.after_cancel(self._vol_job)
        self._vol_job = self.after(120, lambda: self.app.set_volume(self.ident, v))

    def set_state(self, state: str, text: str = ""):
        """state: off | connecting | playing | error"""
        on = state in ("connecting", "playing")
        self.switch_var.set(on)
        self.configure(fg_color=CARD_ON if on else CARD)
        self.icon.configure(text={"off": "🔈", "connecting": "⏳", "playing": "🔊", "error": "⚠️"}[state])
        self.sub.configure(text=text or self._base_text())
        if on:
            self.vol_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        else:
            self.vol_row.grid_forget()


class App(ctk.CTk):
    def __init__(self, start_hidden: bool):
        ctk.set_appearance_mode("system")
        super().__init__(fg_color=BG)
        self.title(APP)
        self.geometry("460x680")
        self.minsize(400, 520)
        try:
            self.iconbitmap(os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(__file__)), "echora.ico"))
        except Exception:
            pass
        self.settings = load_settings()
        lang = self.settings["language"]
        self.lang = windows_language() if lang == "auto" else lang
        self.s = STRINGS[self.lang]
        alac_patch.set_latency_ms(LATENCY_MS[self.settings["latency"]])

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.capture: Capture | None = None
        self.devices = {}   # ident -> config
        self.cards = {}     # ident -> SpeakerCard
        self.sessions = {}  # ident -> dict(atv, pipe, task)
        self.wanted = set(self.settings["selected"])  # speakers the user turned on

        self._build_ui()
        if self.settings["muted_by_us"]:
            self.local_mute(False)  # app was closed/crashed while muting; restore
        if self.settings["autostart"]:
            try:
                set_autostart(True)  # refresh path/name (e.g. after rename or move)
            except OSError:
                pass
        self.persist()
        self._build_tray()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        if start_hidden:
            self.withdraw()
        self.scan()
        self.after(100, self._tick)

    def t(self, key, *args):
        return self.s[key].format(*args) if args else self.s[key]

    # ----- layout
    def _build_ui(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 4))
        ctk.CTkLabel(head, text=APP, font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        self.scan_btn = ctk.CTkButton(head, text=self.t("scan"), width=80, fg_color=ACCENT, command=self.scan)
        self.scan_btn.pack(side="right")

        src = ctk.CTkFrame(self, fg_color="transparent")
        src.pack(fill="x", padx=20, pady=(0, 10))
        self.src_label = ctk.CTkLabel(src, text=self.t("source", "–"), text_color=MUTED, anchor="w")
        self.src_label.pack(side="left")
        self.meter = ctk.CTkProgressBar(src, width=90, height=6, progress_color="#22C55E")
        self.meter.set(0)
        self.meter.pack(side="right")

        ctk.CTkLabel(self, text=self.t("speakers"), text_color=MUTED, anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=22)
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True, padx=12, pady=(2, 6))
        self.empty = ctk.CTkLabel(self.list, text=self.t("searching"), text_color=MUTED)
        self.empty.pack(pady=30)

        opts = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        opts.pack(fill="x", padx=20, pady=(4, 8))
        opts.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opts, text=self.t("latency"), anchor="w").grid(row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        self.lat_labels = {k: self.t(f"lat_{k}") for k in LATENCY_MS}
        self.latency = ctk.CTkSegmentedButton(opts, values=list(self.lat_labels.values()),
                                              selected_color=ACCENT, command=self.on_latency)
        self.latency.set(self.lat_labels[self.settings["latency"]])
        self.latency.grid(row=0, column=1, padx=14, pady=(12, 6), sticky="e")

        ctk.CTkLabel(opts, text=self.t("language"), anchor="w").grid(row=1, column=0, padx=14, pady=6, sticky="w")
        self.lang_labels = {k: (v or self.t("lang_auto")) for k, v in LANG_NAMES.items()}
        self.lang_btn = ctk.CTkSegmentedButton(opts, values=list(self.lang_labels.values()),
                                               selected_color=ACCENT, command=self.on_language)
        self.lang_btn.set(self.lang_labels[self.settings["language"]])
        self.lang_btn.grid(row=1, column=1, padx=14, pady=6, sticky="e")

        self.auto_var = ctk.BooleanVar(value=self.settings["autostart"])
        ctk.CTkSwitch(opts, text=self.t("autostart"), variable=self.auto_var,
                      progress_color=ACCENT, command=self.on_autostart).grid(
            row=2, column=0, columnspan=2, padx=14, pady=6, sticky="w")
        self.mute_var = ctk.BooleanVar(value=self.settings["mute_local"])
        ctk.CTkSwitch(opts, text=self.t("mute_local"), variable=self.mute_var,
                      progress_color=ACCENT, command=self.on_mute_option).grid(
            row=3, column=0, columnspan=2, padx=14, pady=(6, 12), sticky="w")

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w")
        self.status.pack(fill="x", padx=22, pady=(0, 10))

    def _tick(self):
        lvl = self.capture.level if self.capture and self.capture.active() else 0
        self.meter.set(min(1.0, lvl * 1.5))
        self.after(80, self._tick)

    # ----- tray
    def _tray_image(self, active: bool):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, 62, 62), fill=(59, 130, 246) if active else (100, 110, 125))
        for i, r in enumerate((10, 18, 26)):  # sound waves = "echo"
            d.arc((32 - r, 32 - r, 32 + r, 32 + r), -50, 50, fill="white", width=4)
        d.ellipse((14, 26, 26, 38), fill="white")
        return img

    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(self.t("tray_show"), lambda: self.after(0, self.show), default=True),
            pystray.MenuItem(self.t("tray_stop"), lambda: self.after(0, self.stop_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self.t("tray_quit"), lambda: self.after(0, self.quit_app)),
        )
        self.tray = pystray.Icon(APP, self._tray_image(False), APP, menu)
        self.tray.run_detached()

    def _update_tray(self):
        playing = [self.devices[i].name for i, s in self.sessions.items() if s.get("playing")]
        self.tray.icon = self._tray_image(bool(playing))
        self.tray.title = self.t("tray_playing", ", ".join(playing)) if playing else APP

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self):
        self.withdraw()

    # ----- helpers
    def run(self, coro, done=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if done:
            fut.add_done_callback(lambda f: self.after(0, done, f))
        return fut

    def set_status(self, text):
        self.status.configure(text=text)

    def persist(self):
        self.settings["selected"] = sorted(self.wanted)
        save_settings(self.settings)

    # ----- options
    def on_latency(self, label):
        key = next(k for k, v in self.lat_labels.items() if v == label)
        self.settings["latency"] = key
        alac_patch.set_latency_ms(LATENCY_MS[key])
        self.persist()
        for ident in list(self.sessions):  # restart streams with the new buffer
            self.stop(ident)
        self.set_status(self.t("latency_set", label, LATENCY_MS[key] / 1000))

    def on_language(self, label):
        key = next(k for k, v in self.lang_labels.items() if v == label)
        if key == self.settings["language"]:
            return
        self.settings["language"] = key
        self.persist()
        self.quit_app(restart=True)  # rebuild the UI in the new language

    # The capture taps audio *before* Windows' volume, so muting the device
    # silences headphones/DAC but not the AirPlay stream.
    def local_mute(self, on: bool):
        try:
            from pycaw.pycaw import AudioUtilities
            vol = AudioUtilities.GetSpeakers().EndpointVolume
            if on and self.settings["mute_local"] and not self.settings["muted_by_us"]:
                if vol.GetMute():
                    return  # already muted by the user; leave it alone
                vol.SetMute(1, None)
                self.settings["muted_by_us"] = True
            elif not on and self.settings["muted_by_us"]:
                vol.SetMute(0, None)
                self.settings["muted_by_us"] = False
            self.persist()
        except Exception as e:
            self.set_status(self.t("mute_err", e))

    def on_mute_option(self):
        self.settings["mute_local"] = self.mute_var.get()
        if self.capture and self.mute_var.get():
            self.local_mute(True)
        elif not self.mute_var.get():
            self.local_mute(False)
        self.persist()

    def on_autostart(self):
        try:
            set_autostart(self.auto_var.get())
            self.settings["autostart"] = self.auto_var.get()
            self.persist()
        except OSError as e:
            self.auto_var.set(False)
            self.set_status(self.t("autostart_err", e))

    # ----- discovery
    def scan(self):
        self.set_status(self.t("searching"))
        self.scan_btn.configure(state="disabled")
        self.run(pyatv.scan(self.loop, timeout=4, protocol=Protocol.RAOP), self.on_scan)

    def on_scan(self, fut):
        self.scan_btn.configure(state="normal")
        try:
            confs = fut.result()
        except Exception as e:
            self.set_status(self.t("scan_error", e))
            return
        for c in confs:
            self.devices[c.identifier] = c
        if self.devices:
            self.empty.pack_forget()
        for ident, c in sorted(self.devices.items(), key=lambda kv: kv[1].name.lower()):
            if ident not in self.cards:
                card = SpeakerCard(self.list, self, ident, c)
                card.pack(fill="x", padx=6, pady=5)
                self.cards[ident] = card
        if not self.devices:
            self.empty.configure(text=self.t("none_found"))
        self.set_status(self.t("found", len(self.devices)))
        for ident in self.wanted:  # reconnect speakers that were on last time
            if ident in self.devices and ident not in self.sessions:
                self.start(ident)

    # ----- streaming
    def ensure_capture(self) -> bool:
        if self.capture is None:
            try:
                self.capture = Capture()
                self.capture.start()
                self.local_mute(True)
                self.src_label.configure(text=self.t("source", self.capture.name))
            except Exception as e:
                self.set_status(self.t("capture_err", e))
                return False
        return True

    def start(self, ident):
        if ident in self.sessions:
            return
        self.wanted.add(ident)
        self.persist()
        card = self.cards[ident]
        if not self.ensure_capture():
            card.set_state("error", self.t("no_source"))
            return
        conf = self.devices[ident]
        pipe = self.capture.new_source()
        sess = {"pipe": pipe, "atv": None, "task": None, "playing": False}
        self.sessions[ident] = sess
        card.set_state("connecting", self.t("connecting"))
        vol = self.settings["volumes"].get(ident, 40)

        async def go():
            sess["task"] = asyncio.current_task()
            atv = await pyatv.connect(conf, self.loop, protocol=Protocol.RAOP)
            sess["atv"] = atv
            try:
                try:
                    await atv.audio.set_volume(vol)
                except Exception:
                    pass
                sess["playing"] = True
                self.after(0, card.set_state, "playing", self.t("playing"))
                self.after(0, self._update_tray)
                await atv.stream.stream_file(pipe)
            finally:
                atv.close()

        def done(f):
            self.sessions.pop(ident, None)
            if self.capture:
                self.capture.remove_source(pipe)
            err = None if f.cancelled() else f.exception()
            if ident in self.wanted and not self._quitting:
                card.set_state("error" if err else "connecting",
                               f"{self.t('reconnecting')} ({err})" if err else self.t("reconnecting"))
                self.after(3000 if err else 300, lambda: ident in self.wanted and self.start(ident))
            else:
                card.set_state("off")
            self._update_tray()
            self.maybe_stop_capture()

        self.run(go(), done)

    _quitting = False

    def stop(self, ident, user=False):
        if user:
            self.wanted.discard(ident)
            self.persist()
        s = self.sessions.get(ident)
        if s:
            s["pipe"].closed = True
            if s["task"]:
                self.loop.call_soon_threadsafe(s["task"].cancel)
        elif user:
            self.cards[ident].set_state("off")

    def stop_all(self):
        for ident in list(self.wanted | set(self.sessions)):
            self.stop(ident, user=True)

    def maybe_stop_capture(self):
        if not self.sessions and not self.wanted and self.capture:
            self.capture.running = False
            self.capture = None
            self.local_mute(False)

    def set_volume(self, ident, vol):
        self.persist()
        s = self.sessions.get(ident)
        if s and s["atv"]:
            self.run(s["atv"].audio.set_volume(float(vol)))

    def quit_app(self, restart=False):
        self._quitting = True
        self.local_mute(False)
        self.persist()
        for s in list(self.sessions.values()):
            s["pipe"].closed = True
            if s["task"]:
                self.loop.call_soon_threadsafe(s["task"].cancel)
        self.tray.stop()
        if restart:
            subprocess.Popen(launch_args("--wait"))
        self.after(400, self.destroy)


def single_instance(wait: bool) -> bool:
    k32 = ctypes.windll.kernel32
    for _ in range(50 if wait else 1):
        h = k32.CreateMutexW(None, False, "EchoraSingleInstance")
        if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return True
        k32.CloseHandle(h)
        time.sleep(0.1)
    return False


if __name__ == "__main__":
    if not single_instance("--wait" in sys.argv):
        sys.exit(0)
    boost_timing()
    App(start_hidden="--minimized" in sys.argv).mainloop()
