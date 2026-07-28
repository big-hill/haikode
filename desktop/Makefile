# makefile-engine based build for haikode (recommended on Haiku)
# This guarantees resources compile and everything links against real libbe.
#
# On Haiku (with haiku_devel installed):
#     make
#
# Clean:
#     make clean

NAME := haikode
TYPE := APP

APP_MIME_SIG := application/x-vnd.haikode

SRCS := \
	src/app/HaiApplication.cpp \
	src/ui/HaiWindow.cpp \
	src/ui/SettingsWindow.cpp \
	src/domain/AppController.cpp \
	src/domain/ConfigBridge.cpp

RDEFS := resources/haikode.rdef

# BFilePanel lives in libtracker.so. Recursive assignment (=, not :=) so
# $(STDCPPLIBS) is expanded after the makefile-engine defines it below.
LIBS = be tracker $(STDCPPLIBS)

# Make headers visible from the various subdirs
LOCAL_INCLUDE_PATHS := src src/ui src/domain src/app

# Find the real makefile-engine
DEVEL_DIRECTORY := $(shell findpaths -r "makefile_engine" B_FIND_PATH_DEVELOP_DIRECTORY)
include $(DEVEL_DIRECTORY)/etc/makefile-engine

# Notes:
# - This produces a proper native Haiku binary with resources.
# - For the long term we can split domain into a haikode_core static lib
#   (unit testable without a window) per the spec.
