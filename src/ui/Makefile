CC ?= gcc
PROJ := $(abspath ../..)
LVGL := $(PROJ)/third_party/lvgl
CFLAGS := -O2 -DLV_CONF_INCLUDE_SIMPLE -I$(abspath .) -I$(LVGL) -Wno-unused-function
SRC := $(shell find $(LVGL)/src -name '*.c')

.PHONY: all clean

all: cms_ui

cms_ui: main.c $(SRC)
	$(CC) $(CFLAGS) -o $@ main.c $(SRC) -lX11 -lpthread -lm

clean:
	rm -f cms_ui
