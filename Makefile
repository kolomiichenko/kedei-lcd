CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra

.PHONY: all clean install install-desktop uninstall test

all: kedei-lcd

kedei-lcd: src/kedei-lcd.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f kedei-lcd

install:
	sudo ./install.sh ui

install-desktop:
	sudo ./install.sh desktop

uninstall:
	sudo ./install.sh uninstall

test: kedei-lcd
	sudo systemctl stop kedei-ui kedei-lcd 2>/dev/null || true
	sudo ./kedei-lcd test
