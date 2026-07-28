#ifndef HAI_CONFIG_BRIDGE_H
#define HAI_CONFIG_BRIDGE_H

#include <String.h>

// Thin bridge to the Python configtool shared with the haikode CLI.
// We never parse or write the JSON config ourselves (spec: no C++ JSON
// parser); all reads/writes go through:
//     PYTHONPATH=/boot/home/haikode python3 -m haikode.configtool <args>
// (Same install root as the /boot/home/config/non-packaged/bin/haikode
// launcher.)
//
// Contract (implemented in parallel on the Python side):
//     list-providers                       -> "<name>\t<dialect>\t<base_url>\t<model>\t<key:yes|no|n/a>" per line
//     get providers.<name>.base_url        -> value
//     set providers.<name>.base_url <val>  -> "ok"
//     set-key <provider> <secret>          -> "ok keystore" | "ok config"
//     test <provider>                      -> "OK ..." (exit 0) | "FAIL ..." (exit 1)

class ConfigBridge {
public:
	// Runs "configtool <args>" via popen, returns combined stdout+stderr.
	// exitCode receives the tool's exit status (or -1 if it could not run).
	// Blocking — call from a worker thread, not a window thread.
	static	BString			RunConfigTool(const BString& args, int* exitCode);
	// Like RunConfigTool, but writes sensitive input to the child's stdin so
	// it never appears in argv / Deskbar's process list.
	static	BString			RunConfigToolWithInput(const BString& args,
									const BString& input, int* exitCode);

	// Quotes a single argument for safe inclusion in a shell command line.
	static	BString			ShellQuote(const BString& value);
};

#endif
