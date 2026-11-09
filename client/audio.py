"""
Voice messages: microphone recording and playback, all offline.

The Python standard library cannot capture audio, so the recorder uses the
best backend available on this computer, in this order:

1. sounddevice  (pip install sounddevice) - any OS, bundled by PyInstaller
2. Windows MCI  - built into every Windows (winmm.dll via ctypes), no install
3. arecord      - ALSA tool present on most Linux desktops

Recordings are 16 kHz, 16-bit mono WAV (about 32 KB per second), which keeps
voice messages small while staying clear for speech.

Playback of WAV uses winsound on Windows (standard library), and
paplay/aplay on Linux; anything else falls back to the default system player.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import wave

RATE = 16000
MAX_SECONDS = 180


def wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, OSError, EOFError):
        return None


# =========================================================================== recording
class Recorder:
    """start(path) ... stop() -> duration in seconds (or raises RuntimeError)."""

    def __init__(self):
        self.backend = self.detect()
        self.path = None
        self.started = None
        self._impl = None

    @staticmethod
    def detect():
        try:
            import sounddevice  # noqa: F401
            return "sounddevice"
        except Exception:                       # ImportError or missing PortAudio
            pass
        if sys.platform.startswith("win"):
            return "mci"
        if shutil.which("arecord"):
            return "arecord"
        return None

    @property
    def available(self):
        return self.backend is not None

    def start(self, path):
        if self.backend is None:
            raise RuntimeError("No microphone backend found. Install it with: "
                               "pip install sounddevice")
        self.path = path
        self.started = time.monotonic()
        if self.backend == "sounddevice":
            self._impl = _SoundDeviceRec(path)
        elif self.backend == "mci":
            self._impl = _MciRec(path)
        else:
            self._impl = _ARecordRec(path)
        self._impl.start()

    def elapsed(self):
        return 0.0 if self.started is None else time.monotonic() - self.started

    def stop(self):
        if self._impl is None:
            raise RuntimeError("not recording")
        impl, self._impl = self._impl, None
        impl.stop()
        dur = wav_duration(self.path)
        if not dur or dur < 0.3:
            raise RuntimeError("Recording failed or was too short - check the microphone")
        return dur

    def cancel(self):
        if self._impl is not None:
            try:
                self._impl.stop()
            except Exception:
                pass
            self._impl = None
        try:
            os.remove(self.path)
        except (OSError, TypeError):
            pass


class _SoundDeviceRec:
    def __init__(self, path):
        import sounddevice as sd
        self.sd = sd
        self.path = path
        self.frames = []
        self.stream = None

    def start(self):
        def cb(indata, frames, t, status):
            self.frames.append(bytes(indata))
        self.stream = self.sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                             callback=cb)
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()
        with wave.open(self.path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(self.frames))


class _MciRec:
    """Windows Media Control Interface: records straight from the default microphone."""

    def __init__(self, path):
        import ctypes
        self.mci = ctypes.windll.winmm.mciSendStringW
        self.buf = ctypes.create_unicode_buffer(256)
        self.path = path

    def _cmd(self, command):
        err = self.mci(command, self.buf, 255, 0)
        if err:
            raise RuntimeError(f"Windows audio error {err} on '{command.split()[0]}'")

    def start(self):
        self.mci("close wbvoice", None, 0, 0)                    # in case a stale one exists
        self._cmd("open new type waveaudio alias wbvoice")
        # Parameter order as in the classic MCI recording sequence:
        # bitspersample, channels, alignment, samplespersec, bytespersec
        self._cmd(f"set wbvoice bitspersample 16 channels 1 alignment 2 "
                  f"samplespersec {RATE} bytespersec {RATE * 2}")
        self._cmd("record wbvoice")

    def stop(self):
        try:
            self._cmd("stop wbvoice")
            self._cmd(f'save wbvoice "{self.path}"')
        finally:
            self.mci("close wbvoice", None, 0, 0)


class _ARecordRec:
    def __init__(self, path):
        self.path = path
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
             "-d", str(MAX_SECONDS), self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(0.15)
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"arecord failed: {err or 'no microphone?'}")

    def stop(self):
        import signal
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)                 # lets arecord finish the WAV header
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# =========================================================================== playback
class Player:
    """Plays one WAV at a time inside the app; other formats go to the system player."""

    def __init__(self):
        self.proc = None
        self.playing = None
        self.lock = threading.Lock()

    def play(self, path, on_end=None):
        self.stop()
        self.playing = path
        if not path.lower().endswith(".wav"):
            from client.media import open_with_system
            open_with_system(path)
            self.playing = None
            return "system"
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            dur = wav_duration(path) or 0
            self._end_timer(path, dur, on_end)
            return "winsound"
        for tool in ("paplay", "aplay", "afplay"):
            exe = shutil.which(tool)
            if exe:
                with self.lock:
                    self.proc = subprocess.Popen([exe, path], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
                proc = self.proc

                def wait():
                    proc.wait()
                    if self.playing == path:
                        self.playing = None
                        if on_end:
                            on_end()
                threading.Thread(target=wait, daemon=True).start()
                return tool
        from client.media import open_with_system
        open_with_system(path)
        self.playing = None
        return "system"

    def _end_timer(self, path, dur, on_end):
        def done():
            time.sleep(dur + 0.2)
            if self.playing == path:
                self.playing = None
                if on_end:
                    on_end()
        threading.Thread(target=done, daemon=True).start()

    def stop(self):
        self.playing = None
        if sys.platform.startswith("win"):
            try:
                import winsound
                winsound.PlaySound(None, 0)
            except Exception:
                pass
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.proc = None
