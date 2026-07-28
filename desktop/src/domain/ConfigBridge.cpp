#include "ConfigBridge.h"

#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

// Same install root and PYTHONPATH as the CLI launcher
// /boot/home/config/non-packaged/bin/haikode:
//     PYTHONPATH=/boot/home/haikode python3 -m haikode "$@"
static const char* kConfigToolPrefix =
	"PYTHONPATH=/boot/home/haikode python3 -m haikode.configtool ";


/*static*/ BString
ConfigBridge::RunConfigTool(const BString& args, int* exitCode)
{
	if (exitCode != NULL)
		*exitCode = -1;

	BString command(kConfigToolPrefix);
	command << args << " 2>&1";

	FILE* pipe = popen(command.String(), "r");
	if (pipe == NULL)
		return BString("error: could not launch configtool");

	BString output;
	char buffer[1024];
	size_t bytes;
	while ((bytes = fread(buffer, 1, sizeof(buffer), pipe)) > 0)
		output.Append(buffer, bytes);

	int status = pclose(pipe);
	if (exitCode != NULL) {
		if (status != -1 && WIFEXITED(status))
			*exitCode = WEXITSTATUS(status);
		else
			*exitCode = -1;
	}

	return output;
}


/*static*/ BString
ConfigBridge::RunConfigToolWithInput(const BString& args,
	const BString& input, int* exitCode)
{
	if (exitCode != NULL)
		*exitCode = -1;

	char outputPath[] = "/tmp/haikode-configtool-XXXXXX";
	int outputFD = mkstemp(outputPath);
	if (outputFD < 0)
		return BString("error: could not create configtool output file");
	close(outputFD);

	BString command(kConfigToolPrefix);
	command << args << " > " << ShellQuote(outputPath) << " 2>&1";
	FILE* pipe = popen(command.String(), "w");
	if (pipe == NULL) {
		unlink(outputPath);
		return BString("error: could not launch configtool");
	}
	if (!input.IsEmpty())
		fwrite(input.String(), 1, input.Length(), pipe);
	int status = pclose(pipe);

	BString output;
	FILE* result = fopen(outputPath, "r");
	if (result != NULL) {
		char buffer[1024];
		size_t bytes;
		while ((bytes = fread(buffer, 1, sizeof(buffer), result)) > 0)
			output.Append(buffer, bytes);
		fclose(result);
	}
	unlink(outputPath);

	if (exitCode != NULL) {
		if (status != -1 && WIFEXITED(status))
			*exitCode = WEXITSTATUS(status);
		else
			*exitCode = -1;
	}
	return output;
}


/*static*/ BString
ConfigBridge::ShellQuote(const BString& value)
{
	// POSIX single-quote quoting: close quote, escaped quote, reopen.
	BString escaped(value);
	escaped.ReplaceAll("'", "'\\''");

	BString quoted("'");
	quoted << escaped << "'";
	return quoted;
}
