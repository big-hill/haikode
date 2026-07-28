CXX ?= g++
CXXFLAGS ?= -O2 -Wall
TARGET = hai-keystore

$(TARGET): main.cpp
	$(CXX) $(CXXFLAGS) -o $(TARGET) main.cpp -lbe

install: $(TARGET)
	mkdir -p $(HOME)/config/non-packaged/bin
	cp $(TARGET) $(HOME)/config/non-packaged/bin/

clean:
	rm -f $(TARGET)

.PHONY: install clean
