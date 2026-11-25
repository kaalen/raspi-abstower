# Raspberry Pi 5 — ABS Mini Tower (ZP-0128) controller

Hardware: [52Pi ZP-0128 ABS Mini Tower kit](https://wiki.52pi.com/index.php?title=ZP-0128)
(bought cheaply on AliExpress). **It is sold/documented for Raspberry Pi 4** — the vendor's
own installer (`geeekpi/absminitowerkit`, cloned locally at `~/absminitowerkit`) targets Pi 4
and does **not** work as-is on a Pi 5. Everything in this repo (`tower.py`, `tower.service`)
is the result of porting the fan control, OLED display and RGB "mood light" to the Pi 5's
different GPIO/PWM stack. This file is the from-scratch reinstall guide — read it before
re-flashing the OS.

Kit includes: a tower case, a PWM-controlled fan, a 0.91" SSD1306 OLED (I2C), and a ring of
WS281x-style addressable RGB LEDs ("moodlight").

## Why the vendor installer doesn't work on Pi 5

`absminitowerkit/install_bookworm.sh` (the vendor script) assumes:
- RPi.GPIO / BOARD pin numbering works for the fan — **broken on Pi 5** (different GPIO
  chip/controller layout; `RPi.GPIO` doesn't support the Pi 5 at all on this setup).
- `rpi_ws281x` (via `scons`/`cmake`, producing the `/usr/bin/moodlight` binary driven by
  `minitower_moodlight.service`) talks to the LEDs via the old PWM/DMA peripheral — **not
  present/compatible on Pi 5's RP1 southbridge**.
- The OLED script (`sysinfo.py` + `demo_opts.py`, installed under `/usr/local/minitower/`)
  mostly still works since it's plain I2C via `luma.oled`, but it's wired together with the
  two systemd services above rather than one process.

None of the `minitower_moodlight.service` / `minitower_oled.service` units from the vendor
installer are installed on this Pi — they were abandoned in favour of the single custom
`tower.service` below.

## What we actually ended up with

One Python script, [tower.py](tower.py), running as one systemd service
([tower.service](tower.service)), with three threads:

| Part | Old (Pi4/vendor) approach | What works on Pi 5 |
|---|---|---|
| Fan on/off | `RPi.GPIO` BOARD pin 8, software PWM (see [pwm_fan.py](pwm_fan.py), the original vendor-style script, kept only for reference) | `gpiod` (character-device GPIO), line **GPIO14** on **`/dev/gpiochip15`** — see below for why |
| OLED display | `luma.oled` SSD1306 over I2C, driven by vendor's `sysinfo.py` | Same `luma.oled`/`luma.core` stack, just folded into `tower.py` (layout borrowed from `luma.examples`' `sys_info_extended.py`) |
| RGB moodlight | `rpi_ws281x` C library + `/usr/bin/moodlight` binary + PWM/DMA | `adafruit-blinka` + **`Adafruit-Blinka-Raspberry-Pi5-Neopixel`** (`adafruit_raspberry_pi5_neopixel_write.neopixel_write`), driving `board.D18` via the Pi 5's PIO-based NeoPixel support — no custom kernel module needed in the end |

### Dead end that we tried and abandoned: custom RP1 PWM kernel module

Before finding the `Adafruit-Blinka-Raspberry-Pi5-Neopixel` package, a lot of time went into
building a **custom RP1 PWM device-tree overlay + kernel module** (`rp1_ws281x_pwm`) to get
the old `rpi_ws281x` library working via a `/dev/ws281x_pwm` node. That path was fully
built and `insmod`-able but was dropped once the PIO-based Adafruit library worked directly
with no kernel module and no overlay. **It is not part of the current working setup** — full
details of that dead end (kept for reference only, in case the PIO approach ever breaks) are
in [absminitowerkit/NOTES.md](/home/pi/absminitowerkit/NOTES.md).

### GPIO chip/line chasing (the fan)

On Pi 5, GPIO is exposed over several `/dev/gpiochipN` character devices rather than one
`/dev/gpiochip0`, and numbering is **not** the same as Pi 4. We spent a long time guessing
(`gpiochip11`/12/13/14) before confirming via `gpioinfo` which chip actually owns the fan's
control line. The answer that works on this specific board:

- GPIO line: **14**
- chip: **`/dev/gpiochip15`**

`tower.py` has `FAN_CHIP_OVERRIDE = "/dev/gpiochip15"` to skip the chip search on every
boot, but it also has a fallback `find_gpio_chip_for_line()` that scans all `/dev/gpiochip*`
for the line if that device is ever missing (e.g. after a kernel update renumbers chips) —
if the fan stops responding after an OS update, delete/ignore the override and check
`gpioinfo` again.

## Full reinstall procedure (fresh Raspberry Pi OS Bookworm 64-bit on Pi 5)

1. **Flash & boot** Raspberry Pi OS (Bookworm, 64-bit, aarch64) with Raspberry Pi Imager as
   normal, SSH in as `pi`.

2. **Enable I2C** (for the OLED) — either `sudo raspi-config` → Interface Options → I2C →
   enable, or directly add to `/boot/firmware/config.txt`:
   ```
   dtparam=i2c_arm=on
   ```
   Reboot, then confirm the OLED is detected at address `0x3c`:
   ```bash
   sudo apt install -y i2c-tools
   i2cdetect -y 1
   ```

3. **System packages**:
   ```bash
   sudo apt update
   sudo apt install -y git python3-venv python3-dev python3-pip \
       libjpeg-dev zlib1g-dev libfreetype6-dev liblcms2-dev libopenjp2-7 libtiff5-dev \
       i2c-tools gpiod libgpiod-dev
   sudo usermod -a -G gpio,i2c,spi pi   # relogin/reboot for group membership to take effect
   ```

4. **Clone the repos** (this repo plus the upstream examples library the display layout is
   borrowed from):
   ```bash
   cd ~
   git clone https://github.com/rm-hull/luma.examples.git
   # this repo (raspi-abstower) — copy/clone it back to ~/raspi-abstower
   ```

5. **Create the venv and install Python deps**:
   ```bash
   python3 -m venv ~/luma-env
   source ~/luma-env/bin/activate
   cd ~/luma.examples
   pip install -e .                       # luma.core / luma.oled / luma.lcd / luma.led_matrix / luma.emulator
   pip install psutil gpiod
   pip install adafruit-blinka
   pip install Adafruit-Blinka-Raspberry-Pi5-Neopixel
   ```
   (`adafruit-circuitpython-pixelbuf` etc. come in as dependencies automatically.)

6. **Find the fan's real GPIO chip/line** (don't assume `gpiochip15` — verify on each new
   image/kernel):
   ```bash
   sudo apt install -y gpiod   # for the gpioinfo CLI
   gpioinfo | less            # look for the fan's line name/number, note the chip
   ```
   Update `FAN_GPIO` / `FAN_CHIP_OVERRIDE` at the top of `tower.py` if different from
   `14` / `/dev/gpiochip15`.

7. **Copy `tower.py` into place** (it expects `~/luma.examples` to exist, for the font file
   it loads — `DejaVuSansMono.ttf` — and currently lives there itself):
   ```bash
   cp tower.py ~/luma.examples/tower.py
   ```

8. **Install and start the systemd service**:
   ```bash
   sudo cp tower.service /etc/systemd/system/tower.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now tower.service
   systemctl status tower.service
   journalctl -u tower.service -f     # watch fan on/off + any errors
   ```

9. **Sanity checks**:
   - OLED should light up within ~1s showing Temp/CPU/Mem/Disk + uptime/IP.
   - RGB ring should start fading blue → purple → pink.
   - Fan should click on once CPU passes `FAN_ON_TEMP` (48°C) and off below `FAN_OFF_TEMP`
     (45°C) — watch `journalctl -u tower.service -f` while running something CPU-heavy.

## Reference links

- Kit wiki / pinout / original vendor docs: <https://wiki.52pi.com/index.php?title=ZP-0128>
- Vendor installer (Pi 4-oriented, source of the original approach ported here):
  <https://github.com/geeekpi/absminitowerkit> (cloned locally at `~/absminitowerkit`)
- `luma.examples` (OLED display code/fonts this borrows from):
  <https://github.com/rm-hull/luma.examples> (cloned locally at `~/luma.examples`)
- Pi 5 GPIO character-device background:
  <https://emlogic.no/2024/09/linux-drivers-getting-started-with-gpio-on-raspberry-pi-5/>
- `rpi_ws281x` C library (used by the vendor installer, abandoned on Pi 5):
  <https://github.com/jgarff/rpi_ws281x>
- Detailed notes on the abandoned custom RP1 PWM kernel module path:
  [absminitowerkit/NOTES.md](/home/pi/absminitowerkit/NOTES.md)

## Files in this repo

- [tower.py](tower.py) — the actual controller running in production (fan + OLED + RGB, one
  process, three threads).
- [tower.service](tower.service) — systemd unit for `tower.py`.
- [fan_rgb.py](fan_rgb.py) — standalone RGB-only fade script (what `tower.py`'s `run_rgb()`
  thread was extracted from); useful for testing the LED ring in isolation.
- [pwm_fan.py](pwm_fan.py) — the **original vendor-style** fan script using `RPi.GPIO`
  BOARD pin 8 + software PWM. Kept for reference only — **does not work on Pi 5**, superseded
  by the `gpiod`-based fan thread in `tower.py`.
