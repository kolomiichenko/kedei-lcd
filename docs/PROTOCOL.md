# KeDei 3.5" SPI TFTLCD v6.3 protocol

Everything below was recovered from KeDei's own Raspbian kernel
(`LCD_show_v6_1_3`, Linux 4.19.60, driver compiled into the kernel image) by
reading the driver state from `/dev/mem` on a running system, disassembling the
driver functions (`spi_transform`, `check_id`, `iinntt`, `ili9325_set_area`,
`ili9325_update`, `touch_read`, `thread_process`) and emulating the init
function. There is no datasheet.

## Board

- 3 × 74HC595 shift registers (24 bits) feed a parallel 16-bit LCD bus.
- HR2046 (XPT2046 clone) resistive touch controller.
- Only the SPI pins of the 26-pin header are used:

| Signal | Header pin | BCM GPIO | Use |
|---|---|---|---|
| MOSI | 19 | 10 | data |
| MISO | 21 | 9 | touch controller replies |
| CLK | 23 | 11 | clock |
| T_CS | 24 | 8 (CE0) | touch chip select **and** LCD frame latch |
| L_CS | 26 | 7 (CE1) | LCD chip select, held asserted |
| IRQ | 22 | 25 | touch PENIRQ, low while pressed |

There is no D/C line and no separate reset line: both are bits inside the
frame.

## Frames

SPI0, **mode 3** (CPOL=1, CPHA=1). Every command, parameter and pixel is sent
as its own 4-byte frame with L_CS (CE1) asserted:

| Frame | Bytes |
|---|---|
| command | `00 11 00 <cmd>` |
| parameter | `00 15 00 <value>` |
| pixel (RGB565) | `00 15 <hi> <lo>` |
| reset asserted | `00 00 00 00` |
| reset released | `00 01 00 00` |

Bit 0 of the second byte is the LCD reset line, bit 2 is D/C. A frame that
arrives with a slipped bit can therefore reset the panel, which shows up as a
white screen.

For every frame the vendor driver:

1. writes the SPI0 `CS` register = `mode 3 | CS1 | TA | CLEAR_TX | CLEAR_RX`,
2. waits for `TXD`, writes the 4 bytes to the FIFO, waits for `DONE`,
3. pulses T_CS (GPIO8, configured as a plain output) low → low → high.

The T_CS pulse latches the frame into the LCD; without it nothing reaches the
panel.

### Clock

The vendor kernel uses `CDIV = 8`. On Raspberry Pi OS Trixie the core clock
runs at 400 MHz, which makes that 50 MHz, and frames get corrupted (the panel
flashes and resets). `CDIV = 16` (25 MHz at 400 MHz, 15.6 MHz at 250 MHz) is
reliable. A full 480×320 frame then takes ~0.3 s.

## Initialization

### Touch controller wake-up (`check_id`)

The LCD does not respond until the touch controller has been addressed:

1. switch GPIO8 to ALT0 (hardware CE0), `CDIV = 32`, `CS` = mode 3 on CS0;
2. send `00 E7 00 00` 21 times, wait 50 ms, send it 21 more times
   (draining the RX FIFO after each frame);
3. send `00 80 00 00`;
4. switch GPIO8 back to a plain output, set it high.

The vendor driver averages these touch readings into a "panel id" and picks one
of six init sequences from it. The tested board reports `0x10E2`; only that
sequence is implemented here.

### LCD init (panel id 0x10E2)

```
reset released, 50 ms
reset asserted, 100 ms
reset released, 50 ms
CMD 00                                  10 ms
CMD FF, CMD FF                          10 ms
CMD FF × 4                              15 ms
CMD 11 (sleep out)                      150 ms
CMD B0 00
CMD B3 02 00 00 00
CMD B9 01 00 0F 0F
CMD C0 13 3B 00 02 00 01 00 43
CMD C1 08 0F 08 08
CMD C4 11 07 03 04
CMD C6 00
CMD C8 03 03 13 5C 03 07 14 08 00 21 08 14 07 53 0C 13 03 03 21 00
CMD 35 00
CMD 36 60                               landscape 480×320
CMD 3A 55                               RGB565
CMD 44 00 01
CMD D0 07 07 1D 03
CMD D1 03 30 10
CMD D2 03 14 04
CMD 29 (display on)                     30 ms
CMD B4 00
```

## Drawing

```
CMD 2B  y_start(16 bit)  0x013F
CMD 2A  x_start(16 bit)  0x01DF
CMD 2C
pixels…
```

Every 16-bit window value is sent as two parameter frames (high byte, low
byte). The panel does **not** wrap to the next row at the end of a row: the
vendor driver re-sends the window for every row, and so does this driver.

## Touch

Before talking to the touch controller the LCD is "parked", so that bytes
shifted through the 74HC595 chain cannot land in its memory:

```
CMD 2B 01 40 01 40      off-screen window
CMD 2A 01 DF 01 DF
CMD 29                  no memory write in progress
```

Then GPIO8 goes to ALT0, the clock is lowered (the HR2046 is specified up to
~2.5 MHz) and each value is read with a `00 <cmd> 00 00` frame:

| Command | Value |
|---|---|
| `D3` | X |
| `93` | Y |
| `B3` | Z1 (pressure) |
| `80` | power down between conversions, re-enables PENIRQ |

The 12-bit result is `(rx[2] << 5) | (rx[3] >> 3)`. Each value is the mean of 6
conversions without the minimum and the maximum. Afterwards GPIO8 is switched
back to a plain output (high) and the next LCD frame must set the window again.

On the tested board the touch axes are swapped relative to the landscape
screen (raw Y follows screen X, raw X decreases from top to bottom).
