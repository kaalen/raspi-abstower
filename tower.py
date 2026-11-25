#!/usr/bin/env python3
"""Mini Tower controller for Raspberry Pi 5.

Controls three things from one process, each in its own thread:
  - CPU fan (on/off via GPIO, with hysteresis)
  - OLED system info display (SSD1306 over I2C)
  - RGB fan and mood lighting (WS281x on GPIO18 via the Pi 5 PIO driver)

If one part crashes it is restarted after a few seconds; the others keep running.
On SIGTERM/SIGINT (systemd stop, shutdown, Ctrl+C) every part switches its
hardware off before the script exits.
"""

import glob
import logging
import math
import signal
import socket
import threading
from datetime import datetime
from pathlib import Path

import gpiod
import psutil
import board
import adafruit_pixelbuf
from adafruit_raspberry_pi5_neopixel_write import neopixel_write
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont

# ---------------------------------------------------------------- settings --

# Fan
FAN_GPIO = 14
FAN_CHIP_OVERRIDE = "/dev/gpiochip15"
FAN_ON_TEMP = 48.0        # turn fan on above this (°C)
FAN_OFF_TEMP = 45.0       # turn fan off below this (°C); set equal to FAN_ON_TEMP to disable hysteresis
FAN_CHECK_SECONDS = 10

# Display
DISPLAY_I2C_PORT = 1
DISPLAY_I2C_ADDRESS = 0x3C
DISPLAY_REFRESH_SECONDS = 0.5
DISPLAY_FONT_PATHS = [
    "/home/pi/luma.examples/examples/fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
NETWORK_INTERFACE = None  # None = pick automatically, or e.g. "eth0" / "wlan0"
TOGGLE_INTERVAL_SECONDS = 4   # bottom line alternates between uptime and IP

# RGB lighting
LED_PIN = board.D18
LED_COUNT = 16
LED_BRIGHTNESS = 0.3      # 0.0 – 1.0
LED_BYTEORDER = "GRB"
LED_STEP_SECONDS = 6.0    # time to fade from one colour to the next
LED_FPS = 30
LED_COLOURS = [
    (0, 120, 255),   # blue
    (140, 0, 255),   # purple
    (255, 40, 150),  # pink
]

# ------------------------------------------------------------ shared state --

log = logging.getLogger("tower")
stop_event = threading.Event()
state_lock = threading.Lock()
state = {"temp": None, "fan_on": False}


def read_cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read().strip()) / 1000.0


# --------------------------------------------------------------------- fan --

def find_gpio_chip_for_line(gpio_number):
    for chip_path in sorted(glob.glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(chip_path) as chip:
                try:
                    chip.get_line_info(gpio_number)
                    return chip_path
                except (FileNotFoundError, ValueError, OSError):
                    continue
        except OSError:
            continue
    raise FileNotFoundError(f"GPIO{gpio_number} not found on any gpiochip")


def resolve_gpio_chip():
    if glob.glob(FAN_CHIP_OVERRIDE):
        return FAN_CHIP_OVERRIDE
    return find_gpio_chip_for_line(FAN_GPIO)


def run_fan():
    Direction = gpiod.line.Direction
    Value = gpiod.line.Value

    chip_path = resolve_gpio_chip()
    log.info("Using GPIO%d on %s", FAN_GPIO, chip_path)

    with gpiod.Chip(chip_path) as chip:
        request = chip.request_lines(
            {FAN_GPIO: gpiod.LineSettings(direction=Direction.OUTPUT,
                                          output_value=Value.INACTIVE)},
            consumer="FAN",
        )
        fan_on = False
        try:
            while not stop_event.is_set():
                try:
                    temp = read_cpu_temp()
                except (OSError, ValueError):
                    log.exception("Could not read CPU temperature – running fan to be safe")
                    temp = None

                if temp is None:
                    want_on = True
                elif fan_on:
                    want_on = temp > FAN_OFF_TEMP
                else:
                    want_on = temp > FAN_ON_TEMP

                if want_on != fan_on:
                    request.set_values({FAN_GPIO: Value.ACTIVE if want_on else Value.INACTIVE})
                    fan_on = want_on
                    log.info("Fan %s (CPU %s°C)", "on" if fan_on else "off",
                             f"{temp:.1f}" if temp is not None else "?")

                with state_lock:
                    state["temp"] = temp
                    state["fan_on"] = fan_on

                stop_event.wait(FAN_CHECK_SECONDS)
        finally:
            try:
                request.set_values({FAN_GPIO: Value.INACTIVE})
            finally:
                request.release()
                with state_lock:
                    state["fan_on"] = False
            log.info("Fan off")


# ----------------------------------------------------------------- display --

# Layout copied from luma's sys_info_extended.py
MARGIN_Y_LINE = [0, 13, 25, 38, 51]
MARGIN_X_FIGURE = 78
MARGIN_X_BAR = 31
MARGIN_X_FAN = 36
BAR_WIDTH = 52
BAR_WIDTH_FULL = 95
BAR_HEIGHT = 8
BAR_MARGIN_TOP = 3


def load_font(size):
    for path in DISPLAY_FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    log.warning("DejaVuSansMono.ttf not found – using default font")
    return ImageFont.load_default()


def get_uptime():
    uptime = str(datetime.now() - datetime.fromtimestamp(psutil.boot_time())).split(".")[0]
    return f"UpTime: {uptime}"


def find_single_ipv4_address(addrs):
    for addr in addrs:
        if addr.family == socket.AF_INET:
            return addr.address
    return None


def get_ipv4_address(interface_name=None):
    if_addrs = psutil.net_if_addrs()
    if_stats = psutil.net_if_stats()

    if isinstance(interface_name, str) and interface_name in if_addrs:
        return find_single_ipv4_address(if_addrs[interface_name]) or ""

    # Skip loopback, prefer interfaces that are up
    filtered = {
        iface: addrs for iface, addrs in if_addrs.items()
        if not any(a.address == "127.0.0.1" for a in addrs if a.family == socket.AF_INET)
    }
    ordered = [
        iface for iface, _ in sorted(if_stats.items(),
                                     key=lambda x: (x[1].isup, x[1].duplex), reverse=True)
        if iface in filtered
    ]
    for iface in ordered:
        address = find_single_ipv4_address(filtered[iface])
        if address:
            return address
    return ""


def run_display():
    device = ssd1306(i2c(port=DISPLAY_I2C_PORT, address=DISPLAY_I2C_ADDRESS))
    font_default = load_font(12)
    font_full = load_font(10)
    psutil.cpu_percent(None)   # first call primes the counter

    def draw_text(draw, x, line, text):
        draw.text((x, MARGIN_Y_LINE[line]), text, font=font_default, fill="white")

    def draw_percent_row(draw, line, label, percent):
        draw_text(draw, 0, line, label)
        top = MARGIN_Y_LINE[line] + BAR_MARGIN_TOP
        if percent < 100:
            draw_text(draw, MARGIN_X_FIGURE, line, f"{percent:5.1f} %")
            draw.rectangle((MARGIN_X_BAR, top, MARGIN_X_BAR + BAR_WIDTH, top + BAR_HEIGHT),
                           outline="white")
            draw.rectangle((MARGIN_X_BAR, top, MARGIN_X_BAR + BAR_WIDTH * percent / 100,
                            top + BAR_HEIGHT), fill="white")
        else:
            draw.rectangle((MARGIN_X_BAR, top, MARGIN_X_BAR + BAR_WIDTH_FULL, top + BAR_HEIGHT),
                           fill="white")
            draw.text((65, top - 2), "100 %", font=font_full, fill="black")

    try:
        while not stop_event.is_set():
            with state_lock:
                fan_on = state["fan_on"]
            try:
                temp = read_cpu_temp()
            except (OSError, ValueError):
                temp = None

            usage = psutil.disk_usage("/")

            with canvas(device) as draw:
                draw_text(draw, 0, 0, "Temp")
                if fan_on:
                    draw_text(draw, MARGIN_X_FAN, 0, "FAN")
                draw_text(draw, MARGIN_X_FIGURE, 0,
                          f"{temp:5.1f}'C" if temp is not None else "  --'C")

                draw_percent_row(draw, 1, "CPU", psutil.cpu_percent(None))
                draw_percent_row(draw, 2, "Mem", psutil.virtual_memory().percent)
                draw_percent_row(draw, 3, "Disk", usage.used / usage.total * 100)

                if datetime.now().second % (TOGGLE_INTERVAL_SECONDS * 2) < TOGGLE_INTERVAL_SECONDS:
                    draw_text(draw, 0, 4, get_uptime())
                else:
                    draw_text(draw, 0, 4, f"IP: {get_ipv4_address(NETWORK_INTERFACE)}")

            stop_event.wait(DISPLAY_REFRESH_SECONDS)
    finally:
        try:
            device.clear()
            device.hide()
        except Exception:
            pass
        log.info("Display off")


# --------------------------------------------------------------------- rgb --

class Pi5Pixelbuf(adafruit_pixelbuf.PixelBuf):
    def __init__(self, pin, size, **kwargs):
        self._pin = pin
        super().__init__(size=size, **kwargs)

    def _transmit(self, buf):
        neopixel_write(self._pin, buf)


def blend(a, b, t):
    t = (1 - math.cos(t * math.pi)) / 2   # ease in/out
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def run_rgb():
    pixels = Pi5Pixelbuf(LED_PIN, LED_COUNT, auto_write=False,
                         byteorder=LED_BYTEORDER, brightness=LED_BRIGHTNESS)
    frames = max(1, int(LED_STEP_SECONDS * LED_FPS))
    try:
        while not stop_event.is_set():
            for i in range(len(LED_COLOURS)):
                start = LED_COLOURS[i]
                end = LED_COLOURS[(i + 1) % len(LED_COLOURS)]
                for f in range(frames):
                    if stop_event.is_set():
                        return
                    pixels.fill(blend(start, end, f / frames))
                    pixels.show()
                    stop_event.wait(1 / LED_FPS)
    finally:
        pixels.fill((0, 0, 0))
        pixels.show()
        log.info("RGB off")


# -------------------------------------------------------------------- main --

def supervise(target):
    """Run a part; if it crashes, log it and restart after 5 s."""
    while not stop_event.is_set():
        try:
            target()
        except Exception:
            log.exception("Crashed – restarting in 5 s")
            stop_event.wait(5)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(threadName)s: %(message)s")

    def handle_signal(signum, frame):
        log.info("Received signal %d – shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threads = [
        threading.Thread(target=supervise, args=(run_fan,), name="fan"),
        threading.Thread(target=supervise, args=(run_display,), name="display"),
        threading.Thread(target=supervise, args=(run_rgb,), name="rgb"),
    ]
    for t in threads:
        t.start()

    while not stop_event.is_set():
        stop_event.wait(1)

    for t in threads:
        t.join(timeout=10)
    log.info("All parts stopped")


if __name__ == "__main__":
    main()