// SPDX-License-Identifier: GPL-2.0-only
//
// kedei-lcd: userspace driver for the KeDei 3.5" SPI TFTLCD v6.3 (2018/4/9)
// display with resistive touchscreen on Raspberry Pi.
//
// Protocol recovered from KeDei's own kernel (LCD_show_v6_1_3, 4.19.60, panel id 0x10E2):
//   - SPI0 mode 3, LCD on CE1 (asserted by hardware while TA=1), max ~25 MHz
//   - every item is a 4-byte frame: 00 11 00 <cmd> | 00 15 <hi> <lo>
//   - before each frame the SPI CS register is rewritten (clear FIFOs, TA=1),
//     after it the touch CS (CE0, GPIO8) is pulsed low -> high to latch it
//   - the panel does not wrap rows: the window is re-sent for every row
//   - touch: HR2046 on CE0 (GPIO8 switched to ALT0 while reading), pen-down on
//     GPIO25; the LCD is "parked" first so touch bytes do not reach its GRAM
// Kernel SPI must be disabled (no dtparam=spi=on): SPI0 and GPIO are driven
// directly through /dev/mem, so this must run as root.
//
// Usage:
//   kedei-lcd test               init and draw color bars
//   kedei-lcd daemon <file>      init, then keep the screen in sync with <file>
//                                (480x320 RGB565 little-endian raw frame) and
//                                expose the touchscreen as a uinput device;
//                                <file> may be a 480x320x16 framebuffer such as
//                                /dev/fb0 (mirror mode, polled every 40 ms)
//
// Touch calibration: /etc/kedei-touch.conf with "xmin xmax ymin ymax swap_xy"
// (raw HR2046 values at the left/right and top/bottom screen edges).
//
// Environment:
//   KEDEI_SPI_CDIV   SPI clock divider for the LCD (default 16, ~25 MHz at a
//                    400 MHz core clock). Lower values corrupt frames.

#include <fcntl.h>
#include <linux/uinput.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define GPIO_OFFSET 0x200000u
#define SPI0_OFFSET 0x204000u

#define W 480
#define H 320

#define PIN_LCD_CS 7   // CE1
#define PIN_TOUCH_CS 8 // CE0
#define PIN_PENIRQ 25  // low while the screen is pressed

// SPI0 CS register bits
#define CS_SEL1 1u
#define CS_CPHA (1u << 2)
#define CS_CPOL (1u << 3)
#define CS_CLEAR_TX (1u << 4)
#define CS_CLEAR_RX (1u << 5)
#define CS_TA (1u << 7)
#define CS_DONE (1u << 16)
#define CS_TXD (1u << 18)
#define CS_MODE (CS_SEL1 | CS_CPOL | CS_CPHA)
#define CS_TOUCH_MODE (CS_CPOL | CS_CPHA) // CE0

#define TOUCH_CDIV 160 // ~2.5 MHz, HR2046 limit
#define SPI_CDIV_DEFAULT 16 // ~25 MHz at 400 MHz core; faster corrupts frames

static volatile uint32_t *gpio;
static volatile uint32_t *spi; // [0]=CS [1]=FIFO [2]=CLK
static uint32_t spi_cdiv = SPI_CDIV_DEFAULT;

static void gpio_mode(int pin, int mode)
{
	volatile uint32_t *reg = gpio + pin / 10;
	int shift = (pin % 10) * 3;
	*reg = (*reg & ~(7u << shift)) | ((uint32_t)mode << shift);
}

static inline void gpio_set(int pin) { gpio[7] = 1u << pin; }
static inline void gpio_clr(int pin) { gpio[10] = 1u << pin; }

static void msleep(int ms) { usleep(ms * 1000); }

// Mirrors spi_transform() from KeDei's kernel driver.
static void frame(uint8_t b0, uint8_t b1, uint8_t b2, uint8_t b3)
{
	spi[0] = CS_MODE | CS_TA | CS_CLEAR_TX | CS_CLEAR_RX;
	while (!(spi[0] & CS_TXD))
		;
	spi[1] = b0;
	spi[1] = b1;
	spi[1] = b2;
	spi[1] = b3;
	while (!(spi[0] & CS_DONE))
		;
	gpio_clr(PIN_TOUCH_CS);
	__sync_synchronize();
	gpio_clr(PIN_TOUCH_CS);
	gpio_set(PIN_TOUCH_CS);
}

static void cmd(uint8_t c) { frame(0x00, 0x11, 0x00, c); }
static void dat(uint8_t d) { frame(0x00, 0x15, 0x00, d); }

static void cmdv(uint8_t c, int n, const uint8_t *d)
{
	cmd(c);
	for (int i = 0; i < n; i++)
		dat(d[i]);
}
#define CMD(c, ...)                                                            \
	do {                                                                   \
		const uint8_t d_[] = {__VA_ARGS__};                            \
		cmdv(c, sizeof(d_), d_);                                       \
	} while (0)

static void *map(int fd, uint32_t base)
{
	void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, base);
	if (p == MAP_FAILED) {
		perror("mmap");
		exit(1);
	}
	return p;
}

// Same logic as bcm_host_get_peripheral_address(): the SoC "ranges" property
// maps the bus address 0x7e000000 to the CPU physical peripheral base.
static uint32_t peripheral_base(void)
{
	uint8_t r[12];
	uint32_t base = 0x3F000000u; // Pi 2/3 fallback
	FILE *f = fopen("/proc/device-tree/soc/ranges", "rb");

	if (!f)
		return base;
	if (fread(r, 1, sizeof(r), f) == sizeof(r)) {
		uint32_t a = (uint32_t)r[4] << 24 | r[5] << 16 | r[6] << 8 | r[7];
		uint32_t b = (uint32_t)r[8] << 24 | r[9] << 16 | r[10] << 8 | r[11];
		base = a ? a : b;
	}
	fclose(f);
	return base;
}

static void hw_init(void)
{
	uint32_t base = peripheral_base();

	int fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0) {
		perror("/dev/mem");
		exit(1);
	}
	gpio = map(fd, base + GPIO_OFFSET);
	spi = map(fd, base + SPI0_OFFSET);
	close(fd);

	gpio_mode(9, 4);          // ALT0: MISO
	gpio_mode(10, 4);         // ALT0: MOSI
	gpio_mode(11, 4);         // ALT0: SCLK
	gpio_mode(PIN_LCD_CS, 4); // ALT0: CE1
	gpio_mode(PIN_TOUCH_CS, 1);
	gpio_set(PIN_TOUCH_CS);

	spi[2] = spi_cdiv;
}

// Mirrors check_id() from KeDei's driver: the touch controller (CE0) must be
// poked before the LCD answers at all. The vendor driver also derives the
// panel variant from these readings; only the 0x10E2 variant is handled here.
static int touch_frame(uint8_t c)
{
	uint8_t r[4];

	spi[0] = CS_TOUCH_MODE | CS_TA | CS_CLEAR_TX | CS_CLEAR_RX;
	while (!(spi[0] & CS_TXD))
		;
	spi[1] = 0x00;
	spi[1] = c;
	spi[1] = 0x00;
	spi[1] = 0x00;
	while (!(spi[0] & CS_DONE))
		;
	for (int i = 0; i < 4; i++)
		r[i] = spi[1];
	return (r[2] << 5) | (r[3] >> 3);
}

static void touch_wakeup(void)
{
	gpio_mode(PIN_TOUCH_CS, 4); // ALT0: hardware CE0 for the touch controller
	spi[2] = 32;
	touch_frame(0xE7);
	for (int i = 0; i < 20; i++)
		touch_frame(0xE7);
	msleep(50);
	for (int i = 0; i < 21; i++)
		touch_frame(0xE7);
	touch_frame(0x80);
	gpio_mode(PIN_TOUCH_CS, 1);
	gpio_set(PIN_TOUCH_CS);
	spi[2] = spi_cdiv;
	msleep(10);
}

static void lcd_init(void)
{
	touch_wakeup();

	frame(0x00, 0x01, 0x00, 0x00); // release reset
	msleep(50);
	frame(0x00, 0x00, 0x00, 0x00); // assert reset
	msleep(100);
	frame(0x00, 0x01, 0x00, 0x00);
	msleep(50);

	cmd(0x00);
	msleep(10);
	cmd(0xFF);
	cmd(0xFF);
	msleep(10);
	for (int i = 0; i < 4; i++)
		cmd(0xFF);
	msleep(15);
	cmd(0x11);
	msleep(150);
	CMD(0xB0, 0x00);
	CMD(0xB3, 0x02, 0x00, 0x00, 0x00);
	CMD(0xB9, 0x01, 0x00, 0x0F, 0x0F);
	CMD(0xC0, 0x13, 0x3B, 0x00, 0x02, 0x00, 0x01, 0x00, 0x43);
	CMD(0xC1, 0x08, 0x0F, 0x08, 0x08);
	CMD(0xC4, 0x11, 0x07, 0x03, 0x04);
	CMD(0xC6, 0x00);
	CMD(0xC8, 0x03, 0x03, 0x13, 0x5C, 0x03, 0x07, 0x14, 0x08, 0x00, 0x21,
	    0x08, 0x14, 0x07, 0x53, 0x0C, 0x13, 0x03, 0x03, 0x21, 0x00);
	CMD(0x35, 0x00);
	CMD(0x36, 0x60); // landscape 480x320
	CMD(0x3A, 0x55); // RGB565
	CMD(0x44, 0x00, 0x01);
	CMD(0xD0, 0x07, 0x07, 0x1D, 0x03);
	CMD(0xD1, 0x03, 0x30, 0x10);
	CMD(0xD2, 0x03, 0x14, 0x04);
	cmd(0x29);
	msleep(30);
	CMD(0xB4, 0x00);
}

static void window(int x0, int y0, int x1, int y1)
{
	CMD(0x2B, y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF);
	CMD(0x2A, x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF);
	cmd(0x2C);
}

static void push_rows(const uint16_t *fb, int y0, int y1)
{
	for (int y = y0; y <= y1; y++) {
		window(0, y, W - 1, H - 1);
		for (int i = y * W; i < (y + 1) * W; i++)
			frame(0x00, 0x15, fb[i] >> 8, fb[i] & 0xFF);
	}
}


// Trimmed mean of 6 conversions, as the vendor driver does.
static int touch_sample(uint8_t c)
{
	int sum = 0, lo = 4096, hi = -1;

	for (int i = 0; i < 6; i++) {
		int v = touch_frame(c);
		sum += v;
		if (v < lo)
			lo = v;
		if (v > hi)
			hi = v;
	}
	return (sum - lo - hi) / 4;
}

static int pen_down(void) { return !(gpio[13] & (1u << PIN_PENIRQ)); }

static void touch_read(int *x, int *y, int *z)
{
	// Park the LCD: off-screen window and no memory write in progress.
	CMD(0x2B, 0x01, 0x40, 0x01, 0x40);
	CMD(0x2A, 0x01, 0xDF, 0x01, 0xDF);
	cmd(0x29);

	gpio_mode(PIN_TOUCH_CS, 4);
	spi[2] = TOUCH_CDIV;
	*x = touch_sample(0xD3);
	*y = touch_sample(0x93);
	*z = touch_sample(0xB3);
	touch_frame(0x80); // power down between conversions, re-enables PENIRQ
	gpio_mode(PIN_TOUCH_CS, 1);
	gpio_set(PIN_TOUCH_CS);
	spi[2] = spi_cdiv;
}

static struct {
	int xmin, xmax, ymin, ymax, swap;
} cal = {200, 3900, 200, 3900, 0};

#define CAL_PATH "/etc/kedei-touch.conf"

// Re-reads the calibration whenever the file changes.
static void load_calibration(void)
{
	static struct timespec mtime;
	struct stat st;

	if (stat(CAL_PATH, &st) || (st.st_mtim.tv_sec == mtime.tv_sec &&
				    st.st_mtim.tv_nsec == mtime.tv_nsec))
		return;
	mtime = st.st_mtim;
	FILE *f = fopen(CAL_PATH, "r");
	if (!f)
		return;
	if (fscanf(f, "%d %d %d %d %d", &cal.xmin, &cal.xmax, &cal.ymin,
		   &cal.ymax, &cal.swap) != 5)
		fprintf(stderr, "bad " CAL_PATH ", using defaults\n");
	fclose(f);
}

static int scale(int v, int from, int to, int size)
{
	int r = (v - from) * (size - 1) / (to - from);
	return r < 0 ? 0 : r > size - 1 ? size - 1 : r;
}

static int uinput_open(void)
{
	int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
	if (fd < 0) {
		perror("/dev/uinput");
		return -1;
	}
	ioctl(fd, UI_SET_EVBIT, EV_KEY);
	ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH);
	ioctl(fd, UI_SET_EVBIT, EV_ABS);
	ioctl(fd, UI_SET_ABSBIT, ABS_X);
	ioctl(fd, UI_SET_ABSBIT, ABS_Y);
	ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT);
	struct uinput_abs_setup ax = {.code = ABS_X, .absinfo = {.maximum = W - 1}};
	struct uinput_abs_setup ay = {.code = ABS_Y, .absinfo = {.maximum = H - 1}};
	ioctl(fd, UI_ABS_SETUP, &ax);
	ioctl(fd, UI_ABS_SETUP, &ay);
	struct uinput_setup us = {.id = {.bustype = BUS_VIRTUAL, .vendor = 0x4b44, .product = 0x0635}};
	strcpy(us.name, "KeDei Touch");
	ioctl(fd, UI_DEV_SETUP, &us);
	ioctl(fd, UI_DEV_CREATE);
	return fd;
}

static void emit(int fd, int type, int code, int value)
{
	struct input_event ev = {.type = type, .code = code, .value = value};
	if (fd >= 0 && write(fd, &ev, sizeof(ev)) < 0)
		perror("uinput write");
}

// State is also published in /run/kedei/touch: "pressed x y raw_x raw_y z".
static void touch_publish(int pressed, int x, int y, int rx, int ry, int rz)
{
	FILE *f = fopen("/run/kedei/touch.tmp", "w");
	if (!f)
		return;
	fprintf(f, "%d %d %d %d %d %d\n", pressed, x, y, rx, ry, rz);
	fclose(f);
	rename("/run/kedei/touch.tmp", "/run/kedei/touch");
}

static void touch_poll(int ufd, int *down)
{
	int rx, ry, rz;

	if (pen_down()) {
		touch_read(&rx, &ry, &rz);
		if (rz < 50) // too light to be a real press
			return;
		int ax = cal.swap ? ry : rx, ay = cal.swap ? rx : ry;
		int x = scale(ax, cal.xmin, cal.xmax, W);
		int y = scale(ay, cal.ymin, cal.ymax, H);
		emit(ufd, EV_ABS, ABS_X, x);
		emit(ufd, EV_ABS, ABS_Y, y);
		if (!*down)
			emit(ufd, EV_KEY, BTN_TOUCH, 1);
		emit(ufd, EV_SYN, SYN_REPORT, 0);
		touch_publish(1, x, y, rx, ry, rz);
		*down = 1;
	} else if (*down) {
		emit(ufd, EV_KEY, BTN_TOUCH, 0);
		emit(ufd, EV_SYN, SYN_REPORT, 0);
		FILE *f = fopen("/run/kedei/touch", "r");
		int p, x = 0, y = 0;
		if (f) {
			if (fscanf(f, "%d %d %d", &p, &x, &y) != 3)
				x = y = 0;
			fclose(f);
		}
		touch_publish(0, x, y, 0, 0, 0);
		*down = 0;
	}
}

static uint16_t fb[W * H], shadow[W * H];

static void test(void)
{
	static const uint16_t colors[] = {0xF800, 0x07E0, 0x001F, 0xFFFF,
					  0x0000, 0xFFE0, 0x07FF, 0xF81F};
	for (int y = 0; y < H; y++)
		for (int x = 0; x < W; x++)
			fb[y * W + x] = colors[x * 8 / W];
	// Mark the top-left corner so orientation is obvious.
	for (int y = 0; y < 40; y++)
		for (int x = 0; x < 40; x++)
			fb[y * W + x] = 0xFFFF;
	struct timespec a, b;
	clock_gettime(CLOCK_MONOTONIC, &a);
	push_rows(fb, 0, H - 1);
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("full frame: %.2fs\n",
	       (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) / 1e9);
}

static void sync_frame(const char *path, struct timespec *last)
{
	struct stat st;

	if (stat(path, &st))
		return;
	// A framebuffer device (mirror mode) has no useful mtime: always read it.
	if (!S_ISCHR(st.st_mode) && st.st_mtim.tv_sec == last->tv_sec &&
	    st.st_mtim.tv_nsec == last->tv_nsec)
		return;
	FILE *f = fopen(path, "rb");
	if (!f)
		return;
	if (fread(fb, sizeof(fb), 1, f) == 1) {
		*last = st.st_mtim;
		// Push only the rows that changed.
		for (int y = 0; y < H; y++)
			if (memcmp(fb + y * W, shadow + y * W, W * 2))
				push_rows(fb, y, y);
		memcpy(shadow, fb, sizeof(fb));
	}
	fclose(f);
}

static void daemon_loop(const char *path)
{
	struct timespec last = {0};
	int ufd = uinput_open(), down = 0;
	int mirror = !strncmp(path, "/dev/", 5);

	memset(shadow, 0, sizeof(shadow));
	push_rows(shadow, 0, H - 1); // clear to black
	for (int tick = 0;; tick++) {
		touch_poll(ufd, &down);
		if (tick % 10 == 0)
			load_calibration();
		if (tick % (mirror ? 2 : 10) == 0)
			sync_frame(path, &last);
		usleep(20 * 1000);
	}
}

int main(int argc, char **argv)
{
	if (argc < 2 || (strcmp(argv[1], "test") && argc < 3)) {
		fprintf(stderr, "usage: %s test | daemon <file>\n", argv[0]);
		return 2;
	}
	const char *cdiv = getenv("KEDEI_SPI_CDIV");
	if (cdiv && atoi(cdiv) >= 2)
		spi_cdiv = atoi(cdiv) & ~1u; // the divider must be even
	hw_init();
	lcd_init();
	if (!strcmp(argv[1], "test"))
		test();
	else
		daemon_loop(argv[2]);
	return 0;
}
