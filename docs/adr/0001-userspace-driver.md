# ADR 0001: Userspace driver with direct register access

## Status

Accepted

## Context

The KeDei 3.5" v6.3 display is not supported by any mainline driver or
overlay (`piscreen`, `waveshare35a`, `mhs35`, `fbtft` ILI9486 all leave the
screen white). KeDei only ships a patched 4.19 kernel image.

The protocol needs per-frame control that the Linux SPI framework does not
offer cheaply: every 4-byte frame is followed by a pulse on the touch chip
select while the LCD chip select stays asserted, and the touch controller
shares the bus with the LCD through the same shift registers.

Options considered:

1. **Kernel module (fbtft/DRM) using the SPI framework.** One `spi_sync()` and
   GPIO toggle per pixel is ~150k calls per frame, several seconds per frame.
   Also needs DKMS and a rebuild for every kernel update.
2. **Kernel module with direct register access** (what KeDei did). Fast, but
   it fights the in-kernel SPI driver and still needs a rebuild per kernel.
3. **Userspace daemon with `/dev/mem` register access.** Same speed as (2),
   no kernel build, survives kernel updates. Requires root and the kernel SPI
   driver to be disabled.

## Decision

Option 3. `kedei-lcd` drives SPI0/GPIO directly, keeps a shadow copy of the
screen and pushes only changed rows. It takes its picture either from a raw
RGB565 file (written by `kedei-ui` or any other program) or from a 480×320
16-bit framebuffer such as the one vc4 creates with
`video=HDMI-A-1:480x320M@60D`, which gives a full console/X11 display. Touch is
exposed through `uinput`, so X11/libinput and applications see a normal
touchscreen.

## Consequences

- No kernel module, no DKMS, works across kernel upgrades.
- `dtparam=spi=on` must stay disabled; nothing else can use SPI0.
- Runs as root because of `/dev/mem`.
- Mirror mode polls the framebuffer (every 40 ms, ~7 % of one core on a Pi 3
  when idle).
- Only the panel variant with id 0x10E2 is implemented.
