"""Patch pyatv's AirPlay 2 sender to send ALAC instead of raw PCM.

Sonos (and some other AirPlay 2 receivers) accept a raw PCM stream (ct=1)
but silently drop the audio. Apple senders use ALAC (ct=2). We wrap each
PCM packet in an *uncompressed* ALAC frame (escape mode), which every ALAC
decoder accepts and costs almost no CPU.
"""
from pyatv.protocols.raop.protocols import airplayv2
from pyatv.support.http import decode_bplist_from_body
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce
from pyatv import exceptions

ALAC_44100_16_2 = 0x40000

# Buffer the receiver keeps before playing (frames @ 44.1 kHz). pyatv's
# default is 1.5 s; lower = less delay but more risk of dropouts on Wi-Fi.
LATENCY_FRAMES = 44100


def set_latency_ms(ms: int) -> None:
    global LATENCY_FRAMES
    LATENCY_FRAMES = max(11025, int(44100 * ms / 1000))

# 23-bit ALAC frame header: channels-1=1 (3b), 0 (4b), 0 (12b),
# has_size=0 (1b), unused=0 (2b), uncompressed=1 (1b)
_HDR = (1 << 20) | 1


def alac_uncompressed(pcm_be: bytes) -> bytes:
    """Wrap big-endian 16-bit stereo PCM in an uncompressed ALAC frame."""
    nbits = len(pcm_be) * 8
    val = (_HDR << nbits) | int.from_bytes(pcm_be, "big")
    val = (val << 3) | 0b111          # ID_END tag
    total = 23 + nbits + 3
    pad = (-total) % 8
    val <<= pad
    return val.to_bytes((total + pad) // 8, "big")


async def _setup_audio_stream(self, control_client_port: int) -> None:
    if self._verifier is None:
        raise exceptions.InvalidStateError("base stream not set up")
    out_key, _ = self._verifier.encryption_keys(
        airplayv2.EVENTS_SALT, airplayv2.EVENTS_WRITE_INFO, airplayv2.EVENTS_READ_INFO
    )
    shared_secret = out_key[0:32]
    self.context.latency = LATENCY_FRAMES
    setup_resp = await self.rtsp.setup(
        body={
            "streams": [
                {
                    "audioFormat": ALAC_44100_16_2,
                    "audioMode": "default",
                    "controlPort": control_client_port,
                    "ct": 2,  # ALAC
                    "isMedia": True,
                    "latencyMax": max(88200, LATENCY_FRAMES),
                    "latencyMin": min(11025, LATENCY_FRAMES),
                    "shk": shared_secret,
                    "spf": 352,
                    "sr": 44100,
                    "type": 0x60,
                    "supportsDynamicStreamID": False,
                    "streamConnectionID": self.rtsp.session_id,
                }
            ]
        }
    )
    stream = decode_bplist_from_body(setup_resp)["streams"][0]
    self.context.control_port = stream["controlPort"]
    self.context.server_port = stream["dataPort"]
    self._cipher = Chacha20Cipher8byteNonce(shared_secret, shared_secret)


_orig_send = airplayv2.AirPlayV2.send_audio_packet


async def _send_audio_packet(self, transport, rtp_header, audio):
    return await _orig_send(self, transport, rtp_header, alac_uncompressed(audio))


def apply():
    airplayv2.AirPlayV2.setup_audio_stream = _setup_audio_stream
    airplayv2.AirPlayV2.send_audio_packet = _send_audio_packet
