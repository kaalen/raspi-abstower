import math
import signal
import time

import board
import adafruit_pixelbuf
from adafruit_raspberry_pi5_neopixel_write import neopixel_write

LED_PIN = board.D18
LED_COUNT = 16
BRIGHTNESS = 0.3          # 0.0 – 1.0
STEP_SECONDS = 6.0        # time to fade from one colour to the next
FPS = 30

# The loop: blue -> purple -> pink -> back to blue
COLOURS = [
    (0, 120, 255),   # blue
    (140, 0, 255),   # purple
    (255, 40, 150),  # pink
]


class Pi5Pixelbuf(adafruit_pixelbuf.PixelBuf):
    def __init__(self, pin, size, **kwargs):
        self._pin = pin
        super().__init__(size=size, **kwargs)

    def _transmit(self, buf):
        neopixel_write(self._pin, buf)


def blend(a, b, t):
    # Ease in/out so each fade starts and ends gently
    t = (1 - math.cos(t * math.pi)) / 2
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def handle_stop(signum, frame):
    raise KeyboardInterrupt


pixels = Pi5Pixelbuf(LED_PIN, LED_COUNT, auto_write=False,
                     byteorder="GRB", brightness=BRIGHTNESS)
signal.signal(signal.SIGTERM, handle_stop)

try:
    frames = int(STEP_SECONDS * FPS)
    while True:
        for i in range(len(COLOURS)):
            start = COLOURS[i]
            end = COLOURS[(i + 1) % len(COLOURS)]
            for f in range(frames):
                pixels.fill(blend(start, end, f / frames))
                pixels.show()
                time.sleep(1 / FPS)
except KeyboardInterrupt:
    pass
finally:
    pixels.fill((0, 0, 0))
    pixels.show()