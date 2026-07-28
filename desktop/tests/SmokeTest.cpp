// Minimal smoke / build test target for M0 (per spec).
// Domain code (AppController + Messages) can be linked here for unit tests
// without bringing in the full UI.

#include "../src/domain/Messages.h"
#include <stdio.h>

int main()
{
	printf("haikode M0 smoke: message constants defined.\n");
	printf("kMsgSendPrompt = 0x%08x\n", (unsigned)kMsgSendPrompt);
	return 0;
}
