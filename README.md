# Echora

Stream your Windows PC's audio to AirPlay 2 speakers (Sonos, HomePod, AirPlay receivers) in real time.

- Streams whatever is playing on your PC to one or more AirPlay speakers at once
- Works with Sonos (sends ALAC, which Sonos requires)
- Smooth playback: high-quality resampling and no zero-padded packets
- Optionally mutes your PC's own speakers/headphones while streaming
- Per-speaker volume, latency presets (0.5 / 1 / 2 s), tray icon, start with Windows
- Swedish and English UI (follows Windows' language, can be changed in the app)

## Download

Download `Echora.exe` from the [latest release](../../releases/latest) and run it. You don't need to install anything.

Windows may show a SmartScreen warning because the exe isn't signed. Click **More info → Run anyway**.

## Run from source

Requires Python 3.12 on Windows.

```
pip install -r requirements.txt
python echora.py
```

Build the exe:

```
pyinstaller --noconfirm --onefile --windowed --name Echora --icon echora.ico --add-data "echora.ico;." --collect-all pyatv --collect-all customtkinter --collect-all miniaudio --collect-all soxr --collect-all pycaw --collect-all comtypes --collect-data pystray echora.py
```

## How it works

| File | What it does |
|---|---|
| `echora.py` | UI, tray icon, settings, and connections to the speakers |
| `audio_engine.py` | Captures audio via WASAPI loopback, resamples it with soxr to 44.1 kHz, and keeps a ring buffer per speaker |
| `alac_patch.py` | Patches [pyatv](https://github.com/postlund/pyatv)'s AirPlay 2 sender to send ALAC instead of raw PCM, and makes the latency configurable |

Settings are stored in `%APPDATA%\Echora\settings.json`.

## Known limitations

- Latency is at least about 0.5 s, so audio won't line up with video.
- Each speaker gets its own stream. Multiple speakers stay closely in sync, but not as tightly as an Apple multi-room group.

## License

MIT
