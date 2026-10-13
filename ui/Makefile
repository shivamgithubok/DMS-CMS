CC ?= gcc
ROOT := $(abspath ..)
LVGL := $(ROOT)/third_party/lvgl
CFLAGS := -O2 -DLV_CONF_INCLUDE_SIMPLE -I$(ROOT)/ui -I$(LVGL) -Wno-unused-function
SRC := $(shell find $(LVGL)/src -name '*.c')

.PHONY: all clean

all: $(ROOT)/ui/cms_ui

$(ROOT)/ui/cms_ui: main.c $(SRC)
	$(CC) $(CFLAGS) -o $@ main.c $(SRC) -lX11 -lpthread -lm

clean:
	rm -f $(ROOT)/ui/cms_ui
