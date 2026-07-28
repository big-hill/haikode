CC      ?= cc
CFLAGS  ?= -Wall -Werror -std=c99
BUILD   := build

.PHONY: all run clean

all: $(BUILD)/app

$(BUILD)/app: src/main.c src/util.c src/util.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ src/main.c src/util.c

run: $(BUILD)/app
	./$(BUILD)/app

clean:
	rm -rf $(BUILD)
