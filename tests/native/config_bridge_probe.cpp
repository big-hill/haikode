// Headless regression probe: compile with ConfigBridge.cpp and -lbe on Haiku.
#include "ConfigBridge.h"
#include <stdio.h>
#include <string.h>

int main(int argc, char** argv)
{
    if (argc != 2)
        return 2;
    int code = -1;
    BString output;
    if (strcmp(argv[1], "stdin") == 0)
        output = ConfigBridge::RunConfigToolWithInput("stdin", "private input", &code);
    else
        output = ConfigBridge::RunConfigTool(ConfigBridge::ShellQuote(argv[1]), &code);
    fputs(output.String(), stdout);
    return code < 0 ? 127 : code;
}
